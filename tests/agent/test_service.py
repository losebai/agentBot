import asyncio

import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority
from agent_bot.agent.errors import AgentError
from agent_bot.agent.records import AgentRunStore
from agent_bot.agent.service import AgentService
from agent_bot.ai.context import DEFAULT_CONTEXT_MAX_CHARS
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.models import ChatMessage

from .test_engine import Provider, Tools


@pytest.mark.parametrize("code", ["AGENT_TIMEOUT", "AI_TIMEOUT", "AGENT_TOOL_SESSION_EXPIRED"])
@pytest.mark.parametrize("entry", ["qq", "admin"])
@pytest.mark.parametrize("text", [
    "已经生成的正文", "继续下载<\uff5c\uff5cDSML\uff5c\uff5c calls>未执行的工具",
])
async def test_interrupted_output_is_available_only_to_qq(
    agent_database, agent_params, seeded_model, code, entry, text,
):
    """超时只允许 QQ 使用正常正文；伪调用草稿留在失败归档，不作为回复投递。"""
    from agent_bot.agent.contracts import ModelEvent

    class InterruptedProvider(Provider):
        """输出正文后模拟超时，不调用任何外部模型。"""

        async def stream_turn(self, call):
            """保留已输出内容，并在下一次读取时终止执行。"""
            yield ModelEvent(kind="text_delta", delta=text)
            raise AgentError(code, status_code=504)

    service = service_for(agent_database, agent_params)
    service.providers["openai-chat"] = InterruptedProvider()
    with pytest.raises(AgentError) as error:
        await service.complete(request(), ExecutionAuthority(entry=entry))
    assert getattr(error.value, "partial_content", None) == (
        text if entry == "qq" and "DSML" not in text else ""
    )
    run = (await service.records.list_runs()).items[0]
    assert run.status == "failed" and run.error_code == code
    assert (await service.records.get_run(run.execution_id)).content == text
    assert service.active == service.tool_runtime.active == 0


async def test_expired_deadline_still_archives_partial_output(
    agent_database, agent_params, seeded_model, monkeypatch,
):
    """总期限耗尽后保存终态使用独立的有限预算，不遗留 running 记录。"""
    from agent_bot.agent.contracts import ModelEvent

    now = [0.0]
    monkeypatch.setattr("agent_bot.services.request_deadline.monotonic", lambda: now[0])

    class DeadlineProvider(Provider):
        """让请求单调时钟过期，模型替身没有真实等待。"""

        async def stream_turn(self, call):
            """在正文输出后耗尽预算。"""
            yield ModelEvent(kind="text_delta", delta="未完成正文")
            now[0] = 301.0
            raise asyncio.TimeoutError

    service = service_for(agent_database, agent_params)
    service.providers["openai-chat"] = DeadlineProvider()
    with pytest.raises(AgentError):
        await service.complete(request(), ExecutionAuthority(entry="qq"))
    run = (await service.records.list_runs()).items[0]
    assert run.status == "failed" and run.error_code == "AGENT_TIMEOUT"
    assert (await service.records.get_run(run.execution_id)).content == "未完成正文"


@pytest.mark.parametrize("tool_failed", [False, True])
async def test_model_failure_preserves_only_successful_tool_receipt_count(
    agent_database, agent_params, seeded_model, tool_failed,
):
    """模型收尾失败仍携带真实成功回执数量，错误工具不能计入成功，也不会重放。"""
    from agent_bot.agent.contracts import ToolResult

    class FailingFinalProvider(Provider):
        """模拟工具完成后模型协议故障。"""

        async def stream_turn(self, call):
            """工具轮正常执行，收尾轮直接失败。"""
            if call.final_only:
                raise AgentError("AI_RESPONSE_INVALID")
            async for event in super().stream_turn(call):
                yield event

    class ControlledTools(Tools):
        """为成功与失败回执提供独立替身。"""

        async def call(self, call):
            """记录执行次数，成功信息只用于计数，不带入异常文本。"""
            self.invoked.append(call.call_id)
            return ToolResult(call_id=call.call_id, name=call.name,
                              content="private output", is_error=tool_failed)

    tools = ControlledTools()
    service = service_for(agent_database, agent_params, tools=tools)
    service.providers["openai-chat"] = FailingFinalProvider()
    with pytest.raises(AgentError) as caught:
        await service.complete(request(), ExecutionAuthority(entry="qq"))
    assert caught.value.completed_tool_calls == (0 if tool_failed else 1)
    assert caught.value.code == "AI_RESPONSE_INVALID"
    assert len(tools.invoked) == 1 and "private" not in str(caught.value)
    assert service.active == tools.active == 0


def service_for(
    db, params, *, tools=None, records=None, maximum_tool_calls=1,
    maximum_context_chars=DEFAULT_CONTEXT_MAX_CHARS,
):
    """替身模型依赖有限工具预算收尾，从服务端注入工具额度与上下文预算。"""
    return AgentService(
        AgentCatalog(params, connect=db.connect),
        records or AgentRunStore(params, connect=db.connect),
        CredentialResolver(),
        {"openai-chat": Provider()},
        tools or Tools(),
        maximum_tool_calls=maximum_tool_calls,
        maximum_context_chars=maximum_context_chars,
    )


def request():
    return AgentRequest(
        model="local-model", prompt="hello", session_id="s", request_id="r", max_steps=1
    )


@pytest.mark.parametrize("maximum", [120_000, 300_000])
async def test_context_budget_comes_from_service_configuration(
    agent_database, agent_params, seeded_model, maximum,
):
    """超过旧上限的对话由服务配置决定，拒绝发生在模型和副作用工具执行之前。"""
    service = service_for(agent_database, agent_params)
    service.maximum_context_chars = maximum
    incoming = request().model_copy(update={"messages": tuple(
        ChatMessage(role="user", content="资料" * 15_000) for _ in range(5)
    )})
    try:
        if maximum == 120_000:
            with pytest.raises(AgentError, match="AGENT_CONTEXT_TOO_LARGE"):
                await service.complete(incoming, ExecutionAuthority(entry="qq"))
            assert not service.providers["openai-chat"].calls
            assert not service.tool_runtime.invoked
        else:
            result = await service.complete(incoming, ExecutionAuthority(entry="qq"))
            assert result.content == "最终回答" and result.record_saved
            assert len(service.tool_runtime.invoked) == 1
        assert service.active == service.tool_runtime.active == 0
    finally:
        await service.aclose()


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


@pytest.mark.parametrize(
    "second_cache,expected_cache,cache_inputs", [(20, 30, 200), (None, 10, 100)],
)
async def test_multiturn_cache_usage_reaches_single_unified_archive(
    agent_database, agent_params, seeded_model, second_cache, expected_cache, cache_inputs,
):
    """两轮模型用量汇总到一次 Agent 对话，首页不能再重复累加原执行表。"""
    from agent_bot.ai.call_records import CallRecordStore
    from agent_bot.ai.contracts import TokenUsage

    class MeteredProvider(Provider):
        """保留真实工具循环，只为各轮附加确定的模型用量。"""

        async def stream_turn(self, call):
            """第二轮可明确上报或缺失缓存，前一轮的有效用量都必须保留。"""
            async for event in super().stream_turn(call):
                if event.turn is not None:
                    event = event.model_copy(update={"turn": event.turn.model_copy(update={
                        "usage": TokenUsage(prompt_tokens=100, completion_tokens=10,
                                            total_tokens=110, cached_tokens=(
                                                10 if len(self.calls) == 1 else second_cache
                                            )),
                    })})
                yield event

    service = service_for(agent_database, agent_params)
    service.providers["openai-chat"] = MeteredProvider()
    result = await service.complete(
        request(), ExecutionAuthority(entry="admin", allowed_tools=("lookup",)),
    )
    assert result.usage.total_tokens == 220
    assert result.usage.cached_tokens == expected_cache
    assert result.usage.cache_prompt_tokens == cache_inputs
    assert service.active_requests == 0
    stats = await CallRecordStore(agent_params, connect=agent_database.connect).statistics()
    assert stats.conversation_count == 1
    assert stats.total_tokens == 220
    assert stats.cached_tokens == expected_cache
    assert stats.cache_prompt_tokens == cache_inputs
    assert stats.cache_hit_rate == pytest.approx(expected_cache / cache_inputs)


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
