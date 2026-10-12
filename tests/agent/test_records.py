from datetime import datetime

import pytest

from agent_bot.agent.contracts import RunFinish, RunStart, StepWrite
from agent_bot.agent.records import AgentRunStore
from agent_bot.ai.errors import AiError


async def test_run_terminal_transition_and_step_order(agent_database, agent_params):
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
    assert "arguments" not in steps.items[0].model_dump()
    summary = (await store.list_runs()).items[0]
    assert "prompt" not in summary.model_dump()
    assert (await store.get_run(run.execution_id)).content == "answer"


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
