"""显式迁移统一调用记录；旧库只读，按来源去重，输出仅包含计数。"""

import argparse
import asyncio
import json
import re
from contextlib import closing
from importlib.metadata import distribution
from pathlib import Path

import pymysql

from agent_bot.ai.call_records import (
    CallRecordStore,
    call_record_time,
    decision_record,
    legacy_record,
    python_record,
)
from agent_bot.ai.database import query
from agent_bot.bootstrap import database_params, load_bootstrap_config
from agent_bot.database import connect_database, database_connector
from agent_bot.settings import DatabaseParams


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
    parser.add_argument("command", choices=["migrate", "repair-times"])
    parser.add_argument("--init-schema", action="store_true", help="显式安装统一记录增量表")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--legacy-config", help="包含 DatabaseParams MySQL 参数的 JSON 文件")
    group.add_argument("--legacy-jsonl", help="service_ai_session 的 JSONL 导出文件")
    parser.add_argument(
        "--source", help="稳定来源名，如 dispatch-ai:dispatch-sdk:service_ai_session"
    )
    args = parser.parse_args(argv)
    if args.command == "repair-times" and (
        args.init_schema or args.legacy_config or args.legacy_jsonl or args.source
    ):
        parser.error("repair-times 只校正本库时间，不能与初始化或旧库导入选项一起使用")
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
