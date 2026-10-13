"""决策统一归档只保存结果，历史回填幂等且不改变原记录。"""

from types import SimpleNamespace

import pytest

from agent_bot.ai.auto_speak_records import AutoSpeakRecords
from agent_bot.ai.call_records import CallRecordStore
from agent_bot.ai.errors import AiError
from agent_bot.record_migration import backfill_local
from agent_bot.settings import DatabaseParams


async def test_decision_archive_and_historical_backfill(job_database):
    """新决策立即进入系统调用，旧决策可以显式补录且重复执行不重复写入。"""
    params = DatabaseParams("localhost", 3306, "offline", "offline", "offline")
    records = AutoSpeakRecords(params, connect=job_database.connect)
    await records.save(
        session_id="group", request_id="auto-decision-1", mcp_code="decision",
        mcp_revision=1, status="completed", duration_ms=10,
        response=SimpleNamespace(should_speak=True, confidence=0.8),
    )
    store = CallRecordStore(params, connect=job_database.connect)
    page = await store.list_calls(category="system")
    assert len(page.items) == 1 and page.items[0].system_kind == "auto_speak"
    detail = await store.get_call(page.items[0].id)
    assert detail.content is None and detail.request_context is None
    assert detail.data["should_speak"] == 1
    job_database.connection.executescript(
        "DELETE FROM agent_bot_call_record;"
        "CREATE TABLE agent_bot_agent_run AS SELECT * FROM agent_bot_ai_session;"
    )
    first = await backfill_local(store, batch_size=1)
    second = await backfill_local(store, batch_size=1)
    assert first["agent_bot_auto_speak_decision"]["inserted"] == 1
    assert second["agent_bot_auto_speak_decision"]["inserted"] == 0
    assert len((await store.list_calls(category="system")).items) == 1


async def test_decision_archive_failure_rolls_back_both_records(job_database):
    """统一表故障不会留下只写入原表的半次决策。"""
    job_database.connection.execute("DROP TABLE agent_bot_call_record")
    records = AutoSpeakRecords(
        DatabaseParams("localhost", 3306, "offline", "offline", "offline"),
        connect=job_database.connect,
    )
    with pytest.raises(AiError):
        await records.save(
            session_id="s", request_id="r", mcp_code="decision", mcp_revision=None,
            status="cancelled", duration_ms=1, response=None,
        )
    assert job_database.connection.execute(
        "SELECT COUNT(*) FROM agent_bot_auto_speak_decision"
    ).fetchone()[0] == 0
