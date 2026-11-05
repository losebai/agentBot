import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_bot.ai.contracts import ModelCreate, ProviderCreate, TextRequest
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.ollama import OllamaProvider
from agent_bot.ai.providers.transport import ProviderTransport
from agent_bot.ai.service import AiService
from agent_bot.ai.sessions import AiSessionStore
from agent_bot.models import ChatMessage
from agent_bot.services.request_deadline import request_deadline

from .test_providers import ByteStream, stream_body


async def make_service(catalog, ai_params, ai_database, handler):
    provider = await catalog.create_provider(
        ProviderCreate(
            name="local", protocol="ollama", base_url="https://offline.invalid", enabled=True
        )
    )
    await catalog.create_model(
        ModelCreate(
            alias="local", upstream_model="exact/model", provider_id=provider.id, enabled=True
        )
    )
    return AiService(
        catalog,
        AiSessionStore(ai_params, connect=ai_database.connect),
        CredentialResolver(),
        {"ollama": OllamaProvider(ProviderTransport(httpx.MockTransport(handler)))},
    )


def request(timeout=2):
    return TextRequest(
        model="local", session_id="s", request_id="r", prompt="hello", timeout_seconds=timeout
    )


async def test_complete_records_without_losing_result(catalog, ai_params, ai_database):
    service = await make_service(
        catalog,
        ai_params,
        ai_database,
        lambda req: httpx.Response(
            200, json={"model": "exact/model",
                       "message": {"role": "assistant", "content": "已生成的回复"}, "done": True}
        ),
    )
    try:
        result = await service.complete(request())
        assert result.content == "已生成的回复" and result.record_saved
        assert (await service.sessions.get_record(result.execution_id)).content == result.content
        service.sessions.save = AsyncMock(side_effect=RuntimeError("private upstream input"))
        second = await service.complete(request())
        assert second.content == "已生成的回复" and not second.record_saved
    finally:
        await service.aclose()


@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("maximum", [120_000, 300_000])
async def test_plain_context_budget_is_checked_before_upstream_call(
    catalog, ai_params, ai_database, streaming, maximum,
):
    """普通及流式入口按运行预算校验总量，大于旧限制的合法请求可以正常发送。"""
    sent = []

    def handle(req):
        """只记录离线请求；额度不足时不应进入传输层。"""
        sent.append(req)
        if streaming:
            return httpx.Response(200, stream=ByteStream(stream_body("ollama")))
        return httpx.Response(200, json={"model": "exact/model", "message": {
            "role": "assistant", "content": "已生成回复",
        }, "done": True})

    service = await make_service(catalog, ai_params, ai_database, handle)
    service.maximum_context_chars = maximum
    incoming = TextRequest(
        model="local", session_id="s", request_id="r", prompt="继续", messages=tuple(
            ChatMessage(role="user", content="资料" * 15_000) for _ in range(5)
        ),
    )
    try:
        if streaming:
            events = [e async for e in service.stream(incoming)]
            if maximum == 120_000:
                assert events[-1].code == "AI_CONTEXT_TOO_LARGE"
            else:
                assert events[-1].kind == "done"
        elif maximum == 120_000:
            with pytest.raises(AiError, match="AI_CONTEXT_TOO_LARGE"):
                await service.complete(incoming)
        else:
            assert (await service.complete(incoming)).content == "已生成回复"
        assert len(sent) == (0 if maximum == 120_000 else 1)
        assert service.active_requests == 0
    finally:
        await service.aclose()


@pytest.mark.parametrize("operation", ["complete", "discovery"])
async def test_active_conversations_exclude_discovery_and_release_on_cancel(
    catalog, ai_params, ai_database, operation,
):
    """真实请求挂起时观察活动计数，模型发现不计为对话，取消后必须归还。"""
    entered = asyncio.Event()

    async def pause(req):
        """停在隔离传输中等待取消，不发送任何外部请求。"""
        entered.set()
        await asyncio.Event().wait()

    service = await make_service(catalog, ai_params, ai_database, pause)
    pending = asyncio.create_task(service.complete(request()) if operation == "complete" else
                                  service.upstream_models(1, 1))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert service.active_requests == (1 if operation == "complete" else 0)
    finally:
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert service.active_requests == 0
        await service.aclose()


async def test_deadline_is_total_and_nested_budget_shrinks(catalog, ai_params, ai_database):
    async def slow(req):
        await asyncio.sleep(1)
        return httpx.Response(200, json={})

    service = await make_service(catalog, ai_params, ai_database, slow)
    try:
        with request_deadline(0.02), pytest.raises(AiError, match="AI_TIMEOUT"):
            await service.complete(request())
    finally:
        await service.aclose()


async def test_stream_terminal_and_saved_content(catalog, ai_params, ai_database):
    service = await make_service(
        catalog,
        ai_params,
        ai_database,
        lambda req: httpx.Response(200, stream=ByteStream(stream_body("ollama"))),
    )
    try:
        events = [event async for event in service.stream(request())]
        assert [event.kind for event in events] == ["start", "delta", "delta", "done"]
        record = await service.sessions.get_record(events[-1].metadata.execution_id)
        assert record.content == "你好" and record.status == "completed"
    finally:
        await service.aclose()


async def test_close_stream_records_cancelled(catalog, ai_params, ai_database):
    stream = ByteStream(stream_body("ollama"))
    service = await make_service(
        catalog, ai_params, ai_database, lambda req: httpx.Response(200, stream=stream)
    )
    generator = service.stream(request())
    try:
        assert (await anext(generator)).kind == "start"
        assert (await anext(generator)).delta == "你"
        await generator.aclose()
        records = await service.sessions.list_records(session_id="s", status="cancelled")
        assert len(records.items) == 1
        assert (await service.sessions.get_record(records.items[0].execution_id)).content == "你"
        assert stream.closed
    finally:
        await generator.aclose()
        await service.aclose()
