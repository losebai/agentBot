"""显式迁移统一调用记录；旧库只读，按来源去重，输出仅包含计数。"""

import argparse
import asyncio
import json
import re
from contextlib import closing
from importlib.metadata import distribution
from pathlib import Path

import pymysql

from agent_bot.ai.call_records import CallRecordStore, legacy_record, python_record
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
    """分批回填本库两个来源，不修改原始会话、步骤或附件。"""
    result = {}
    for table, type in (("agent_bot_ai_session", "ordinary"), ("agent_bot_agent_run", "agent")):
        after, scanned, inserted = "", 0, 0
        while True:
            rows = await store.run(
                lambda c, table=table, after=after: query(
                    c,
                    f"SELECT * FROM {table} WHERE execution_id>%s ORDER BY execution_id LIMIT %s",
                    (after, batch_size),
                )
            )
            if not rows:
                break
            inserted += await store.import_batch([python_record(row, type) for row in rows])
            scanned += len(rows)
            after = rows[-1]["execution_id"]
        result[table] = {"scanned": scanned, "inserted": inserted}
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
    """命令行只接收配置文件路径，连接密码不出现在参数或异常输出中。"""
    parser = argparse.ArgumentParser(prog="agent-bot records")
    parser.add_argument("command", choices=["migrate"])
    parser.add_argument("--init-schema", action="store_true", help="显式安装统一记录增量表")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--legacy-config", help="包含 DatabaseParams MySQL 参数的 JSON 文件")
    group.add_argument("--legacy-jsonl", help="service_ai_session 的 JSONL 导出文件")
    parser.add_argument(
        "--source", help="稳定来源名，如 dispatch-ai:dispatch-sdk:service_ai_session"
    )
    args = parser.parse_args(argv)
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
