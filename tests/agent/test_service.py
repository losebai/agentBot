import asyncio

import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority
from agent_bot.agent.errors import AgentError
from agent_bot.agent.records import AgentRunStore
from agent_bot.agent.service import AgentService
from agent_bot.ai.credentials import CredentialResolver

from .test_engine import Provider, Tools


def service_for(db, params, *, tools=None, records=None):
    return AgentService(
        AgentCatalog(params, connect=db.connect),
        records or AgentRunStore(params, connect=db.connect),
        CredentialResolver(),
        {"openai-chat": Provider()},
        tools or Tools(),
    )


def request():
    return AgentRequest(
        model="local-model", prompt="hello", session_id="s", request_id="r", max_steps=1
    )


async def test_failed_save_does_not_replay_work(agent_database, agent_params, seeded_model):
    class BrokenStore(AgentRunStore):
        async def finish(self, run):
            raise AgentError("AI_DATABASE_UNAVAILABLE", status_code=503)

    service = service_for(
        agent_database,
        agent_params,
        records=BrokenStore(agent_params, connect=agent_database.connect),
    )
    result = await service.complete(
        request(), ExecutionAuthority(entry="admin", allowed_tools=("lookup",))
    )
    assert result.content == "最终回答" and not result.record_saved
    assert len(service.tool_runtime.invoked) == 1


async def test_disconnect_after_tool_side_effect_does_not_retry(
    agent_database, agent_params, seeded_model
):
    happened = asyncio.Event()

    class SlowTools(Tools):
        async def call(self, call):
            self.invoked.append(call.call_id)
            happened.set()
            await asyncio.Event().wait()

    tools = SlowTools()
    service = service_for(agent_database, agent_params, tools=tools)
    task = asyncio.create_task(
        service.complete(request(), ExecutionAuthority(entry="admin", allowed_tools=("lookup",)))
    )
    await asyncio.wait_for(happened.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert tools.invoked == ["c1"] and tools.active == 0 and service.active == 0
    run = (await service.records.list_runs()).items[0]
    assert run.status == "cancelled"
    assert (await service.records.list_steps(run.execution_id)).items[-1].status == "unknown"


async def test_busy_ninth_call_rejected(agent_database, agent_params, seeded_model):
    class BlockingProvider(Provider):
        async def stream_turn(self, call):
            await asyncio.Event().wait()
            yield

    service = service_for(agent_database, agent_params)
    service.providers["openai-chat"] = BlockingProvider()
    authority = ExecutionAuthority(entry="admin", allowed_tools=("lookup",))
    tasks = [asyncio.create_task(service.complete(request(), authority)) for _ in range(8)]
    try:
        for _ in range(100):
            if service.active == 8:
                break
            await asyncio.sleep(0.01)
        with pytest.raises(AgentError) as error:
            await service.complete(request(), authority)
        assert error.value.status_code == 429
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert service.active == 0


@pytest.mark.parametrize("entry", ["admin", "qq"])
async def test_unsupported_preset_is_rejected_before_execution(
    agent_database, agent_params, seeded_model, entry
):
    """两个入口均拒绝未实现的预设工具，避免过滤后静默调用模型。"""
    from agent_bot.agent.contracts import AgentCreate

    service = service_for(agent_database, agent_params)
    await service.catalog.create_agent(
        AgentCreate(
            code="legacy",
            name="legacy",
            model_id=seeded_model[2].id,
            tools=("soloncode",),
            enabled=True,
        )
    )
    with pytest.raises(AgentError, match="AGENT_CAPABILITY_NOT_MIGRATED"):
        await service.complete(
            AgentRequest(agent_code="legacy", prompt="hello", session_id="s", request_id="r"),
            ExecutionAuthority(entry=entry),
        )
    assert service.providers["openai-chat"].calls == []
