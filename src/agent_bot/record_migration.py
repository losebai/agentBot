"""显式迁移统一调用记录；旧库只读，按来源去重，输出仅包含计数。"""

import argparse
import asyncio
import json
import re
from contextlib import closing
from importlib.metadata import distribution
from pathlib import Path
from typing import Any

import pymysql

from agent_bot.ai.call_records import (
    CallRecordStore,
    call_record_time,
    decision_record,
    insert_record,
    json_value,
    legacy_record,
    python_record,
    source_record_time,
)
from agent_bot.ai.database import AiDatabase, query
from agent_bot.ai.errors import AiError
from agent_bot.bootstrap import database_params, load_bootstrap_config
from agent_bot.database import connect_database, database_connector
from agent_bot.settings import DatabaseParams


def _source_available(connection: Any, table: str, key: str) -> bool:
    """明确识别已退役的来源表；仅缺表可跳过，连接或缺列错误仍向外报告。"""
    try:
        query(connection, f"SELECT {key} FROM {table} LIMIT 0")
    except pymysql.DatabaseError as error:
        if error.args[0] != 1146:
            raise
        return False
    return True


def _verify_ordinary_archive(old: dict, archived: dict) -> None:
    """核对正文、执行身份与模型用量，已有快照和分类只保留、不重写。"""
    expected = python_record(old, "ordinary")
    for field in ("session_Id", "request_id", "content", "replay_context", "model",
                  "duration", "execution_id", "status", "type"):
        if archived[field] != expected[field]:
            raise AiError("AI_RECORD_MIGRATION_CONFLICT", status_code=409)
    if (archived["create_time"] is None
            or source_record_time(archived["create_time"]) != expected["create_time"]):
        raise AiError("AI_RECORD_MIGRATION_CONFLICT", status_code=409)
    actual_data, expected_data = json_value(archived["data"]), expected["data"]
    actual_full = actual_data.get("full_fields", {})
    expected_full = expected_data.get("full_fields", {})
    if any(actual_full.get(key) != value for key, value in expected_full.items()):
        raise AiError("AI_RECORD_MIGRATION_CONFLICT", status_code=409)
    for field, original in (("content", "prompt"), ("replay_context", "content"),
                            ("session_Id", "session_id"), ("request_id", "request_id"),
                            ("model", "model")):
        if actual_full.get(field, archived[field]) != old[original]:
            raise AiError("AI_RECORD_MIGRATION_CONFLICT", status_code=409)
    actual_inner = actual_data.get("original_data", actual_data)
    expected_data = expected_data.get("original_data", expected_data)
    for key in ("usage", "configured_model", "error_code"):
        # 详情优先读取顶层；拒绝双层冲突，也避免 MySQL JSON null 与 SQLite NULL 分歧。
        if (key in actual_data and key in actual_inner
                and actual_data[key] != actual_inner[key]):
            raise AiError("AI_RECORD_MIGRATION_CONFLICT", status_code=409)
        if actual_data.get(key, actual_inner.get(key)) != expected_data.get(key):
            raise AiError("AI_RECORD_MIGRATION_CONFLICT", status_code=409)


async def merge_ai_sessions(store: AiDatabase, batch_size: int = 100) -> dict[str, int]:
    """显式逐批合并旧普通调用，缺失则补录；已有记录只补精确 UTC，冲突整批回滚。"""
    result = {"scanned": 0, "inserted": 0, "updated": 0}
    available = await store.run(
        lambda c: _source_available(c, "agent_bot_ai_session", "execution_id"),
    )
    if not available:
        return result
    after = ""
    while True:
        def merge_batch(connection: Any, after: str = after) -> tuple[list[dict], int, int]:
            """源和目标同一事务读取，任何不一致都不留下本批的部分补录。"""
            query(connection, "SELECT created_at_utc FROM agent_bot_call_record LIMIT 0")
            rows = query(connection,
                         "SELECT * FROM agent_bot_ai_session WHERE execution_id>%s "
                         "ORDER BY execution_id LIMIT %s", (after, batch_size))
            inserted = updated = 0
            for row in rows:
                archive = query(connection,
                                "SELECT * FROM agent_bot_call_record WHERE source=%s "
                                "AND source_id=%s FOR UPDATE",
                                ("python:agent_bot_ai_session", row["execution_id"]), one=True)
                if archive is None:
                    insert_record(connection, python_record(row, "ordinary"))
                    inserted += 1
                    continue
                _verify_ordinary_archive(row, archive)
                stamp = source_record_time(row["created_at"])
                if (archive["created_at_utc"] is None
                        or source_record_time(archive["created_at_utc"]) != stamp):
                    with connection.cursor() as cursor:
                        cursor.execute("UPDATE agent_bot_call_record SET created_at_utc=%s "
                                       "WHERE id=%s", (stamp, archive["id"]))
                    updated += 1
            return rows, inserted, updated

        rows, inserted, updated = await store.run(merge_batch, write=True)
        if not rows:
            break
        result["scanned"] += len(rows)
        result["inserted"] += inserted
        result["updated"] += updated
        after = rows[-1]["execution_id"]
    return result


def install_schema(params):
    """仅安装本次新增表，普通应用启动不会隐式执行 DDL。"""
    suffix = ".sqlite.sql" if params.backend == "sqlite" else ".sql"
    name = "2026-10-13-unified-call-records" + suffix
    path = distribution("agent-bot").locate_file("agent_bot/resources/" + name)
    sql = path.read_text(encoding="utf-8")
    with closing(connect_database(params)) as connection:
        with connection.cursor() as cursor:
            # 注释可能含分号，先移除行注释再分割，脚本没有含分号的字面值。
            clean = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
            for statement in clean.split(";"):
                if statement.strip():
                    cursor.execute(statement)
        connection.commit()


async def backfill_local(store, batch_size=100):
    """分批回填会话、Agent 和决策来源，不修改原始记录。"""
    result = {}
    for table, type in (
        ("agent_bot_ai_session", "ordinary"), ("agent_bot_agent_run", "agent"),
        ("agent_bot_auto_speak_decision", "auto_speak"),
    ):
        key = "id" if type == "auto_speak" else "execution_id"
        if table == "agent_bot_ai_session" and not await store.run(
            lambda c, table=table, key=key: _source_available(c, table, key),
        ):
            result[table] = {"scanned": 0, "inserted": 0}
            continue
        after, scanned, inserted = "", 0, 0
        while True:
            rows = await store.run(
                lambda c, table=table, after=after, key=key: query(
                    c,
                    f"SELECT * FROM {table} WHERE {key}>%s ORDER BY {key} LIMIT %s",
                    (after, batch_size),
                )
            )
            if not rows:
                break
            inserted += await store.import_batch([
                decision_record(row) if type == "auto_speak" else python_record(row, type)
                for row in rows
            ])
            scanned += len(rows)
            after = rows[-1][key]
        result[table] = {"scanned": scanned, "inserted": inserted}
    return result


async def repair_local_times(store: CallRecordStore, batch_size: int = 100) -> dict:
    """分批按原始 UTC 时间校正统一记录，重跑不累加偏移，不改正文和旧库记录。"""
    result = {}
    for table, key in (
        ("agent_bot_ai_session", "execution_id"),
        ("agent_bot_agent_run", "execution_id"),
        ("agent_bot_auto_speak_decision", "id"),
    ):
        if table == "agent_bot_ai_session" and not await store.run(
            lambda c, table=table, key=key: _source_available(c, table, key),
        ):
            result[table] = {"scanned": 0, "updated": 0}
            continue
        source = f"python:{table}"
        after, scanned, updated = 0, 0, 0
        while True:
            def repair_batch(connection, table=table, key=key, source=source, after=after):
                """同一事务读取原始时间并仅更新不同值，取消或失败时整批回滚。"""
                rows = query(
                    connection,
                    "SELECT c.id,r.created_at FROM agent_bot_call_record c "
                    f"JOIN {table} r ON c.source_id=r.{key} "
                    "WHERE c.source=%s AND c.id>%s ORDER BY c.id LIMIT %s",
                    (source, after, batch_size),
                )
                changed = 0
                with connection.cursor() as cursor:
                    for row in rows:
                        stamp = call_record_time(row["created_at"])
                        changed += cursor.execute(
                            "UPDATE agent_bot_call_record SET create_time=%s "
                            "WHERE id=%s AND (create_time IS NULL OR create_time<>%s)",
                            (stamp, row["id"], stamp),
                        )
                return rows, changed

            rows, changed = await store.run(repair_batch, write=True)
            if not rows:
                break
            scanned += len(rows)
            updated += changed
            after = rows[-1]["id"]
        result[table] = {"scanned": scanned, "updated": updated}
    return result


async def import_mysql(store, source_params, source, batch_size=100):
    """旧库事务显式只读，以起始最大主键为边界，逐批提交目标事务。"""
    after, scanned, inserted = 0, 0, 0
    with closing(
        connect_database(
            source_params,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=30,
        )
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
        connection.begin()
        ceiling = query(connection, "SELECT MAX(id) AS id FROM service_ai_session", one=True)["id"]
        while ceiling is not None and after < ceiling:
            rows = query(
                connection,
                "SELECT * FROM service_ai_session WHERE id>%s AND id<=%s ORDER BY id LIMIT %s",
                (after, ceiling, batch_size),
            )
            if not rows:
                break
            inserted += await store.import_batch([legacy_record(row, source) for row in rows])
            scanned += len(rows)
            after = rows[-1]["id"]
            if scanned % 5000 == 0:
                print(f"旧库扫描 {scanned} 条，新增 {inserted} 条", flush=True)
        connection.rollback()
    return {"scanned": scanned, "inserted": inserted, "ceiling": ceiling}


async def import_jsonl(store, path, source, batch_size=100):
    """逐行读取可信导出文件；非法记录使当前批失败，不丢弃错误行。"""
    scanned, inserted, batch = 0, 0, []
    # 离线命令没有并发服务；文件逐行读取，避免一次加载整个历史导出。
    with Path(path).open(encoding="utf-8-sig") as stream:  # noqa: ASYNC230
        for line in stream:
            if not line.strip():
                continue
            batch.append(legacy_record(json.loads(line), source))
            scanned += 1
            if len(batch) == batch_size:
                inserted += await store.import_batch(batch)
                batch = []
        inserted += await store.import_batch(batch)
    return {"scanned": scanned, "inserted": inserted}


def run(argv):
    """显式导入或校正归档时间，连接密码不出现在参数或异常输出中。"""
    parser = argparse.ArgumentParser(prog="agent-bot records")
    parser.add_argument("command", choices=["migrate", "repair-times", "merge-ai-sessions"])
    parser.add_argument("--init-schema", action="store_true", help="显式安装统一记录增量表")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--legacy-config", help="包含 DatabaseParams MySQL 参数的 JSON 文件")
    group.add_argument("--legacy-jsonl", help="service_ai_session 的 JSONL 导出文件")
    parser.add_argument(
        "--source", help="稳定来源名，如 dispatch-ai:dispatch-sdk:service_ai_session"
    )
    args = parser.parse_args(argv)
    if args.command in {"repair-times", "merge-ai-sessions"} and (
        args.init_schema or args.legacy_config or args.legacy_jsonl or args.source
    ):
        parser.error("校正或合并命令不能与初始化或旧库导入选项一起使用")
    if (args.legacy_config or args.legacy_jsonl) and (
        not args.source
        or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,191}", args.source)
        or args.source.startswith("python:")
    ):
        parser.error("旧系统导入必须指定非 python: 开头的稳定 --source")
    try:
        params = database_params(load_bootstrap_config())
        if args.init_schema:
            install_schema(params)
        store = CallRecordStore(params, connect=database_connector(params))
        if args.command == "repair-times":
            print(json.dumps(asyncio.run(repair_local_times(store)), ensure_ascii=False))
            return 0
        if args.command == "merge-ai-sessions":
            print(json.dumps(asyncio.run(merge_ai_sessions(store)), ensure_ascii=False))
            return 0
        source_params = None
        if args.legacy_config:
            cfg = json.loads(Path(args.legacy_config).read_text(encoding="utf-8-sig"))
            source_params = DatabaseParams(**cfg)
            if source_params.backend != "mysql":
                raise ValueError("legacy source must be mysql")

        async def migrate():
            """本库与旧库共享来源唯一键，重复执行仅补入未存在的记录。"""
            print(json.dumps(await backfill_local(store), ensure_ascii=False), flush=True)
            if source_params:
                result = await import_mysql(store, source_params, args.source)
                print(json.dumps(result), flush=True)
            elif args.legacy_jsonl:
                print(json.dumps(await import_jsonl(store, args.legacy_jsonl, args.source)))

        asyncio.run(migrate())
    except Exception as error:
        print(f"迁移失败 ({type(error).__name__})；已提交批次保留，修复后使用同一来源名重试。")
        return 1
    return 0
