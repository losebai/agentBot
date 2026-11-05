"""分类索引保留历史识别规则，并让精确计数不再读取归档 JSON。"""

import json
import sqlite3
from pathlib import Path

import pytest

from agent_bot.ai.call_records import CallRecordStore, _call_conditions, legacy_record
from agent_bot.sql_scripts import split_sql

ROOT = Path(__file__).resolve().parents[2] / "sql"


@pytest.mark.parametrize("category,kind,total", [
    ("conversation", None, 2), ("system", None, 3),
    ("system", "command", 2), ("system", "auto_speak", 1),
    ("conversation", "command", 0),
])
async def test_indexed_count_matches_pages_after_record_changes(
    ai_params, ai_database, category, kind, total,
):
    """写入、JSON 更新和删除即时更新索引，不依赖缓存或重新归档。"""
    store = CallRecordStore(ai_params, connect=ai_database.connect)
    await store.import_batch([
        legacy_record({"id": number, "session_Id": "indexed", "replay_context": "ok"}, "test")
        for number in range(6)
    ])
    connection = ai_database.connection
    connection.execute(
        "UPDATE agent_bot_call_record SET data=? WHERE source_id IN ('0','1')",
        (json.dumps({"system_kind": "command"}),),
    )
    connection.execute(
        "UPDATE agent_bot_call_record SET request_id='auto-decision-old' WHERE source_id='2'"
    )
    connection.execute("DELETE FROM agent_bot_call_record WHERE source_id='5'")
    connection.commit()
    filters = dict(category=category, system_kind=kind, session_id="indexed", status="completed")
    assert (await store.count_calls(**filters)).total == total
    assert len((await store.list_calls(**filters, page=1)).items) == total
    assert (await store.count_calls(category=category, system_kind=kind)).total == total


def test_count_uses_classification_index_without_json_functions(ai_database):
    """检查真实执行计划和字节码，避免只改返回值却仍在计数时全表解析。"""
    connection = ai_database.connection
    for category, kind in (("conversation", None), ("system", None),
                           ("system", "command"), ("system", "auto_speak")):
        _, conditions, args = _call_conditions(None, None, None, category, kind)
        sql = "SELECT COUNT(*) FROM agent_bot_call_record WHERE " + " AND ".join(conditions)
        sql = sql.replace("%s", "?")
        plan = connection.execute("EXPLAIN QUERY PLAN " + sql, args).fetchall()
        assert any("idx_call_system_time" in row[3] for row in plan)
        instructions = connection.execute("EXPLAIN " + sql, args).fetchall()
        assert not any("json_extract" in str(row[5]).lower() for row in instructions)


def test_increment_and_fresh_database_have_identical_classification():
    """旧库原地升级与新库生成列、索引一致，MySQL 和 SQLite 分类表达式相同。"""
    sqlite_script = (ROOT / "call-record-classification.sqlite.sql").read_text("utf-8")
    mysql_script = (ROOT / "call-record-classification.sql").read_text("utf-8")
    expression = sqlite_script.split("GENERATED ALWAYS AS (", 1)[1].split(") VIRTUAL", 1)[0]
    assert expression == mysql_script.split("GENERATED ALWAYS AS (", 1)[1].split(") VIRTUAL", 1)[0]
    with sqlite3.connect(":memory:") as connection:
        connection.executescript((ROOT / "agent_bot_init.sqlite.sql").read_text("utf-8"))
        columns = connection.execute("PRAGMA table_xinfo(agent_bot_call_record)").fetchall()
        indexes = connection.execute("PRAGMA index_info(idx_call_system_time)").fetchall()
        connection.execute("DROP INDEX idx_call_system_time")
        connection.execute("ALTER TABLE agent_bot_call_record DROP COLUMN system_kind")
        connection.execute(
            "INSERT INTO agent_bot_call_record(type,source,source_id,status,data) "
            "VALUES ('ordinary','old','1','completed',?)",
            (json.dumps({"system_kind": "command"}),),
        )
        for statement in split_sql(sqlite_script):
            connection.execute(statement)
        assert connection.execute("PRAGMA table_xinfo(agent_bot_call_record)").fetchall() == columns
        assert connection.execute("PRAGMA index_info(idx_call_system_time)").fetchall() == indexes
        assert connection.execute("SELECT system_kind FROM agent_bot_call_record").fetchall() == [
            ("command",),
        ]
