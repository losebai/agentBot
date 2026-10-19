"""普通执行旧表迁移仅使用隔离替身，覆盖全字段保留、冲突和删除后重跑。"""

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from agent_bot import record_migration
from agent_bot.ai.call_records import insert_record, python_record
from agent_bot.ai.contracts import SessionWrite, TokenUsage
from agent_bot.ai.database import sql_columns
from agent_bot.ai.errors import AiError
from agent_bot.ai.sessions import AiSessionStore
from agent_bot.database import database_connector
from agent_bot.settings import DatabaseParams


def read_sql(name: str) -> str:
    """测试夹具同步读取小型 SQL，不在服务事件循环运行。"""
    return Path("sql", name).read_text("utf-8")


def old_record() -> SessionWrite:
    """保留微秒及超长正文，防止迁移只检查来源主键。"""
    return SessionWrite(
        execution_id="e" * 32, session_id="s", request_id="r", configured_model="alias",
        model="upstream", status="failed", duration_ms=15,
        usage=TokenUsage(prompt_tokens=3, completion_tokens=4, total_tokens=7),
        created_at=datetime(2026, 10, 20, 1, 2, 3, 987654), error_code="AI_TIMEOUT",
        prompt="长输入" * 25000, content="长输出" * 25000,
    )


async def seed_old(store: AiSessionStore, record: SessionWrite) -> None:
    """仅显式写入旧表，模拟尚未合并的历史部署。"""
    values = record.model_dump(exclude={"request_context"})
    values["usage"] = record.usage.model_dump_json()

    def write(connection):
        """旧表写入仍使用参数绑定，正文不出现在 SQL 或异常中。"""
        with connection.cursor() as cursor:
            cursor.execute(
                f"INSERT INTO agent_bot_ai_session ({sql_columns(values)}) VALUES ("
                + ",".join("%s" for _ in values) + ")", tuple(values.values()),
            )

    await store.run(write, write=True)


async def test_merge_inserts_missing_and_survives_source_removal(ai_params, ai_database):
    """补录全部原文字段和 UTC 微秒，重跑不重复，删旧表后读取保持一致。"""
    merge = getattr(record_migration, "merge_ai_sessions", None)
    assert callable(merge), "缺少普通执行表合并迁移"
    store = AiSessionStore(ai_params, connect=ai_database.connect)
    record = old_record()
    await seed_old(store, record)
    assert await merge(store, batch_size=1) == {"scanned": 1, "inserted": 1, "updated": 0}
    assert await merge(store, batch_size=1) == {"scanned": 1, "inserted": 0, "updated": 0}
    ai_database.connection.execute("DROP TABLE agent_bot_ai_session")
    assert (await store.get_record(record.execution_id)).model_dump() == record.model_dump(
        exclude={"request_context"},
    )
    assert await merge(store) == {"scanned": 0, "inserted": 0, "updated": 0}


async def test_merge_keeps_existing_id_context_and_classification(ai_params, ai_database):
    """已有归档仅补原始 UTC 时间，不覆盖请求快照或指令分类。"""
    merge = getattr(record_migration, "merge_ai_sessions", None)
    assert callable(merge), "缺少普通执行表合并迁移"
    store = AiSessionStore(ai_params, connect=ai_database.connect)
    record = old_record()
    await seed_old(store, record)
    archive = python_record(record.model_dump(), "ordinary", {"messages": ["保留快照"]})
    archive.pop("created_at_utc")
    archive["data"]["system_kind"] = "command"
    await store.run(lambda c: insert_record(c, archive), write=True)
    before = ai_database.connection.execute("SELECT * FROM agent_bot_call_record").fetchone()
    assert await merge(store) == {"scanned": 1, "inserted": 0, "updated": 1}
    after = ai_database.connection.execute("SELECT * FROM agent_bot_call_record").fetchone()
    assert dict(before) | {"created_at_utc": after["created_at_utc"]} == dict(after)
    assert (await store.get_record(record.execution_id)).created_at == record.created_at


async def test_merge_conflict_rolls_back_batch(ai_params, ai_database):
    """同来源归档正文不同必须明确报错，不能按主键存在就删除旧原文。"""
    merge = getattr(record_migration, "merge_ai_sessions", None)
    assert callable(merge), "缺少普通执行表合并迁移"
    store = AiSessionStore(ai_params, connect=ai_database.connect)
    record = old_record()
    await seed_old(store, record)
    await seed_old(store, record.model_copy(update={"execution_id": "a" * 32}))
    await store.save(record.model_copy(update={"prompt": "不同正文"}))
    before = ai_database.connection.execute("SELECT * FROM agent_bot_call_record").fetchone()
    with pytest.raises(AiError, match="AI_RECORD_MIGRATION_CONFLICT"):
        await merge(store)
    assert dict(before) == dict(ai_database.connection.execute(
        "SELECT * FROM agent_bot_call_record",
    ).fetchone())
    assert ai_database.connection.execute(
        "SELECT COUNT(*) FROM agent_bot_call_record",
    ).fetchone()[0] == 1


async def test_merge_rejects_overflow_metadata_overriding_short_original(ai_params, ai_database):
    """兼容扩展不能偷偷改变短原文，即使主表字段一致也必须拒绝清理。"""
    store = AiSessionStore(ai_params, connect=ai_database.connect)
    record = old_record().model_copy(update={"prompt": "短输入", "content": "短输出"})
    await seed_old(store, record)
    archive = python_record(record.model_dump(), "ordinary")
    archive["data"]["full_fields"] = {"content": "不同的完整原文"}
    await store.run(lambda c: insert_record(c, archive), write=True)
    with pytest.raises(AiError, match="AI_RECORD_MIGRATION_CONFLICT"):
        await record_migration.merge_ai_sessions(store)


@pytest.mark.parametrize(("field", "value"), [
    ("configured_model", "不同别名"), ("usage", {"total_tokens": 999}), ("error_code", None),
])
async def test_merge_rejects_conflicting_root_metadata(
    ai_params, ai_database, field, value,
):
    """顶层元数据不能覆盖长记录的原模型、用量或错误，JSON 空值歧义也必须拒绝。"""
    store = AiSessionStore(ai_params, connect=ai_database.connect)
    record = old_record()
    await seed_old(store, record)
    archive = python_record(record.model_dump(), "ordinary")
    archive["data"][field] = value
    await store.run(lambda c: insert_record(c, archive), write=True)
    with pytest.raises(AiError, match="AI_RECORD_MIGRATION_CONFLICT"):
        await record_migration.merge_ai_sessions(store)


async def test_sqlite_upgrade_merge_retire_matches_fresh_schema(tmp_path):
    """真实 SQLite 从旧字段增量升级、迁移并删表后，结构和读写与新库一致。"""
    path = tmp_path / "merge.sqlite3"
    current = read_sql("agent_bot_init.sqlite.sql")
    with sqlite3.connect(path) as connection:
        connection.executescript(current)
        connection.executescript(read_sql("legacy/ai-session-v1.sqlite.sql"))
        expected_columns = list(connection.execute("PRAGMA table_info(agent_bot_call_record)"))
        expected_indexes = list(connection.execute("PRAGMA index_info(idx_call_source_created)"))
        connection.executescript(
            "DROP INDEX idx_call_source_created;"
            "ALTER TABLE agent_bot_call_record DROP COLUMN created_at_utc;",
        )
        connection.executescript(read_sql("2026-10-20-merge-ai-sessions.sqlite.sql"))
        # 增量新增列的位置不同不影响有效结构，比较名称、类型、默认值和约束。
        assert sorted(row[1:] for row in expected_columns) == sorted(
            row[1:] for row in connection.execute("PRAGMA table_info(agent_bot_call_record)")
        )
        actual_indexes = connection.execute("PRAGMA index_info(idx_call_source_created)")
        assert [(row[0], row[2]) for row in expected_indexes] == [
            (row[0], row[2]) for row in actual_indexes
        ]
    params = DatabaseParams(backend="sqlite", sqlite_path=str(path))
    store = AiSessionStore(params, connect=database_connector(params))
    record = old_record()
    await seed_old(store, record)
    await record_migration.merge_ai_sessions(store)
    with sqlite3.connect(path) as connection:
        connection.executescript(read_sql("2026-10-20-retire-ai-session.sqlite.sql"))
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='agent_bot_ai_session'",
        ).fetchone() is None
    assert (await store.get_record(record.execution_id)).model_dump() == record.model_dump(
        exclude={"request_context"},
    )
    await store.save(record.model_copy(update={"execution_id": "f" * 32}))
    assert len((await store.list_records()).items) == 2
