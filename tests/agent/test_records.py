from datetime import datetime

import pytest

from agent_bot.agent.contracts import RunFinish, RunStart, StepWrite
from agent_bot.agent.records import AgentRunStore
from agent_bot.ai.call_records import CallRecordStore
from agent_bot.ai.contracts import TokenUsage
from agent_bot.ai.errors import AiError
from agent_bot.record_migration import backfill_local
from ai.mysql_identifiers import reject_unquoted_usage


async def test_run_terminal_transition_and_step_order(agent_database, agent_params):
    """终态与工具步骤顺序保持稳定，旧步骤明确返回空负载。"""
    store = AgentRunStore(agent_params, connect=agent_database.connect)
    run = RunStart(
        execution_id="a" * 32,
        session_id="s",
        request_id="r",
        entry="admin",
        model="test",
        prompt="private prompt",
        created_at=datetime.now(),
    )
    await store.begin(run)
    for sequence in [1, 2, 3]:
        await store.append_step(
            StepWrite(
                execution_id=run.execution_id,
                sequence=sequence,
                turn=1,
                kind="tool_result",
                tool="lookup",
                call_id="c1",
                status="completed",
            )
        )
    await store.finish(
        RunFinish(execution_id=run.execution_id, status="completed", content="answer")
    )
    with pytest.raises(AiError) as duplicate:
        await store.finish(RunFinish(execution_id=run.execution_id, status="cancelled"))
    assert duplicate.value.status_code == 409
    steps = await store.list_steps(run.execution_id)
    assert [s.sequence for s in steps.items] == [1, 2, 3]
    assert steps.items[0].arguments is None and steps.items[0].result is None
    summary = (await store.list_runs()).items[0]
    assert "prompt" not in summary.model_dump()
    assert (await store.get_run(run.execution_id)).content == "answer"
    unified = CallRecordStore(agent_params, connect=agent_database.connect)
    summary = (await unified.list_calls(type="agent")).items[0]
    detail = await unified.get_call(summary.id)
    assert detail.execution_id == run.execution_id
    assert detail.content == "private prompt" and detail.replay_context == "answer"
    assert detail.status == "completed"


async def test_keyset_pages_same_timestamp(agent_database, agent_params):
    store = AgentRunStore(agent_params, connect=agent_database.connect)
    stamp = datetime.now()
    for char in "abc":
        await store.begin(
            RunStart(
                execution_id=char * 32,
                session_id="s",
                request_id=char,
                entry="qq",
                model="test",
                prompt="hello",
                created_at=stamp,
            )
        )
    one = await store.list_runs(limit=2)
    two = await store.list_runs(limit=2, cursor=one.next_cursor)
    assert len({r.execution_id for r in one.items + two.items}) == 3
    assert all(r.status == "running" for r in one.items + two.items)


async def test_backfill_preserves_steps_and_live_terminal_state(agent_database, agent_params):
    """空决策目录不影响回填；原步骤及运行结束后的状态不能被迁移覆盖。"""
    agent_database.connection.executescript("""
        CREATE TABLE agent_bot_auto_speak_decision (
            id TEXT PRIMARY KEY, session_id TEXT, request_id TEXT, mcp_code TEXT,
            mcp_revision INTEGER, status TEXT, should_speak INTEGER,
            confidence REAL, duration_ms INTEGER, error_code TEXT, created_at TEXT);
    """)
    store = AgentRunStore(agent_params, connect=agent_database.connect)
    run = RunStart(
        execution_id="e" * 32,
        session_id="s",
        request_id="r",
        entry="admin",
        model="m",
        prompt="旧输入",
        created_at=datetime(2026, 10, 13),
    )
    await store.begin(run)
    await store.append_step(
        StepWrite(
            execution_id=run.execution_id, sequence=1, turn=1, kind="model", status="completed"
        )
    )
    agent_database.connection.execute("DELETE FROM agent_bot_call_record")
    agent_database.connection.commit()
    unified = CallRecordStore(agent_params, connect=agent_database.connect)
    result = await backfill_local(unified, batch_size=1)
    assert result["agent_bot_agent_run"]["inserted"] == 1
    await store.finish(
        RunFinish(execution_id=run.execution_id, status="completed", content="新回复")
    )
    result = await backfill_local(unified, batch_size=1)
    assert result["agent_bot_agent_run"]["inserted"] == 0
    detail = await unified.get_call((await unified.list_calls()).items[0].id)
    assert detail.status == "completed" and detail.replay_context == "新回复"
    assert len((await store.list_steps(run.execution_id)).items) == 1


@pytest.mark.parametrize("mysql_semantics", [False, True], ids=["sqlite", "mysql"])
async def test_reserved_usage_roundtrips_run_lifecycle(
    agent_database, agent_params, monkeypatch, mysql_semantics
):
    """Agent 的开始、终态更新和摘要读取均保留真实 token 用量。"""
    if mysql_semantics:
        reject_unquoted_usage(monkeypatch)
    store = AgentRunStore(agent_params, connect=agent_database.connect)
    execution_id = "d" * 32
    await store.begin(
        RunStart(
            execution_id=execution_id,
            session_id="usage-session",
            request_id="request",
            entry="admin",
            model="test",
            prompt="hello",
            created_at=datetime(2026, 10, 12),
        )
    )
    await store.append_step(
        StepWrite(execution_id=execution_id, sequence=1, turn=1, kind="model", status="completed")
    )
    await store.finish(
        RunFinish(
            execution_id=execution_id,
            status="completed",
            content="answer",
            usage=TokenUsage(prompt_tokens=4, completion_tokens=3, total_tokens=7),
        )
    )
    summary = (await store.list_runs(session_id="usage-session")).items[0]
    detail = await store.get_run(execution_id)
    assert summary.status == detail.status == "completed"
    assert summary.usage.total_tokens == detail.usage.total_tokens == 7
    assert detail.content == "answer"
    assert [step.sequence for step in (await store.list_steps(execution_id)).items] == [1]
