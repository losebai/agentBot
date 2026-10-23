"""真实 LangChain 客户端通过离线 HTTP 验证项目的协议及配置边界。"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import aclosing
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_bot.ai.errors import AiError
from ai.test_providers import ByteStream, call, response, stream_body


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses", "ollama"])
async def test_protocol_and_empty_credentials_are_explicit(protocol, monkeypatch):
    """数据库空凭据不能被 SDK 的环境密钥覆盖，路径必须保留自定义前缀。"""
    from agent_bot.ai.providers.langchain import LangChainProvider

    monkeypatch.setenv("OPENAI_API_KEY", "environment-key-must-not-be-used")
    monkeypatch.setenv("OLLAMA_API_KEY", "environment-key-must-not-be-used")
    monkeypatch.setenv("OPENAI_ORG_ID", "environment-org")
    monkeypatch.setenv("OPENAI_PROJECT_ID", "environment-project")
    requests = []

    def handle(request):
        """只记录发往离线供应商的实际请求。"""
        requests.append(request)
        return httpx.Response(200, json=response(protocol))

    provider = LangChainProvider(protocol, transport=httpx.MockTransport(handle))
    try:
        result = await provider.complete(call(protocol))
        assert result.content == "你好\n\n世界"
        assert len(requests) == 1
        assert "authorization" not in requests[0].headers
        assert "openai-organization" not in requests[0].headers
        assert "openai-project" not in requests[0].headers
        assert json.loads(requests[0].content)["model"] == "Qwen/X"
        expected = {
            "openai-chat": "/v1/chat/completions",
            "openai-responses": "/v1/responses",
            "ollama": "/v1/api/chat",
        }
        assert requests[0].url.path == expected[protocol]
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses", "ollama"])
async def test_upstream_failure_retries_three_times_then_raises(
    protocol: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一模型请求首次失败后最多重试三次，仍失败只返回脱敏错误。"""
    from agent_bot.ai.providers.langchain import LangChainProvider

    monkeypatch.setattr("agent_bot.ai.providers.langchain_http.sleep", AsyncMock(), raising=False)
    requests = []

    def handle(request):
        """模拟限流，检查重试结束后没有额外框架重试。"""
        requests.append(request)
        return httpx.Response(429, text="private upstream secret")

    provider = LangChainProvider(protocol, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(AiError) as error:
            await provider.complete(call(protocol))
        assert error.value.code == "AI_RATE_LIMITED"
        assert "private" not in str(error.value)
        assert len(requests) == 4
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses", "ollama"])
async def test_langchain_stream_closes_on_consumer_disconnect(protocol):
    """消费者提前停止时关闭供应商流，而不是继续读取和执行。"""
    from agent_bot.ai.providers.langchain import LangChainProvider

    raw = ByteStream(stream_body(protocol))
    provider = LangChainProvider(
        protocol, transport=httpx.MockTransport(lambda _: httpx.Response(200, stream=raw))
    )
    try:
        async with aclosing(provider.stream(call(protocol))) as events:
            assert (await anext(events)).content == "你"
        assert raw.closed
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses", "ollama"])
@pytest.mark.parametrize("streaming", [False, True])
async def test_model_succeeds_on_last_retry_without_changing_payload(
    protocol: str, streaming: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """普通和Agent流式模型均可在第三次重试恢复，正文与模型保持同一快照。"""
    from agent_bot.ai.providers.langchain import LangChainProvider

    monkeypatch.setattr("agent_bot.ai.providers.langchain_http.sleep", AsyncMock())
    payloads: list[bytes] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """只模拟供应商前三次暂时不可用，第四次返回完整有效响应。"""
        payloads.append(request.content)
        if len(payloads) < 4:
            return httpx.Response(503, text="private upstream detail")
        if streaming:
            return httpx.Response(200, stream=ByteStream(stream_body(protocol)))
        return httpx.Response(200, json=response(protocol))

    provider = LangChainProvider(protocol, transport=httpx.MockTransport(handle))
    try:
        if streaming:
            events = [event async for event in provider.stream(call(protocol))]
            assert any(event.content for event in events)
        else:
            assert (await provider.complete(call(protocol))).content == "你好\n\n世界"
        assert len(payloads) == 4 and all(payload == payloads[0] for payload in payloads)
    finally:
        await provider.aclose()


@pytest.mark.parametrize("status", [400, 401, 403])
async def test_model_permanent_rejection_does_not_retry(status: int) -> None:
    """参数或凭据被明确拒绝时不能通过重复请求增加调用量。"""
    from agent_bot.ai.providers.langchain import LangChainProvider

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """记录真实HTTP次数，不替换模型调用逻辑。"""
        requests.append(request)
        return httpx.Response(status, text="private credential detail")

    provider = LangChainProvider("openai-chat", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(AiError):
            await provider.complete(call("openai-chat"))
        assert len(requests) == 1
    finally:
        await provider.aclose()


async def test_model_connection_failure_uses_same_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连接尚未建立时也允许三次重试，不能交给SDK另起一轮重试。"""
    from agent_bot.ai.providers.langchain import LangChainProvider

    monkeypatch.setattr("agent_bot.ai.providers.langchain_http.sleep", AsyncMock())
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """模拟前三次连接失败、第四次恢复。"""
        requests.append(request)
        if len(requests) < 4:
            raise httpx.ConnectError("private host", request=request)
        return httpx.Response(200, json=response("openai-chat"))

    provider = LangChainProvider("openai-chat", transport=httpx.MockTransport(handle))
    try:
        assert (await provider.complete(call("openai-chat"))).content == "你好\n\n世界"
        assert len(requests) == 4
    finally:
        await provider.aclose()


async def test_model_retries_stop_when_total_deadline_is_spent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次请求耗尽总预算时不能等待或重试，重试不能重置单次模型的截止时间。"""
    from agent_bot.ai.providers.langchain import LangChainProvider

    now = [0.0]
    requests: list[httpx.Request] = []
    monkeypatch.setattr("agent_bot.ai.providers.langchain_http.monotonic", lambda: now[0])

    def handle(request: httpx.Request) -> httpx.Response:
        """用单调时钟模拟请求已消耗全部预算，不实际等待。"""
        requests.append(request)
        now[0] = 1.0
        return httpx.Response(503)

    provider = LangChainProvider("openai-chat", transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(AiError) as caught:
            request = call("openai-chat")
            await provider.complete(request.model_copy(update={
                "request": request.request.model_copy(update={"timeout_seconds": 0.1}),
            }))
        assert caught.value.code == "AI_TIMEOUT" and len(requests) == 1
    finally:
        await provider.aclose()


async def test_cancelling_model_backoff_stops_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    """重试退避期间取消后立即归还资源，后台不能继续发起请求。"""
    from agent_bot.ai.providers.langchain import LangChainProvider

    entered = asyncio.Event()
    responses: list[httpx.Response] = []

    async def pause(seconds: float) -> None:
        """只替换退避时钟，保持真实模型SDK与HTTP请求。"""
        entered.set()
        await asyncio.Event().wait()

    def handle(request: httpx.Request) -> httpx.Response:
        """返回必须关闭后才进入退避的临时错误响应。"""
        result = httpx.Response(503)
        responses.append(result)
        return result

    monkeypatch.setattr("agent_bot.ai.providers.langchain_http.sleep", pause)
    provider = LangChainProvider("openai-chat", transport=httpx.MockTransport(handle))
    task = asyncio.create_task(provider.complete(call("openai-chat")))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(responses) == 1 and responses[0].is_closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await provider.aclose()


async def test_model_stream_failure_after_text_does_not_repeat_request() -> None:
    """上游已经返回正文后中断不能重放请求，以免重复正文或产生另一批工具调用。"""
    from agent_bot.ai.providers.langchain import LangChainProvider

    class InterruptedStream(httpx.AsyncByteStream):
        """供应商返回一个有效文本片段后断开连接。"""

        async def __aiter__(self) -> AsyncIterator[bytes]:
            """仅替换网络读取边界，保留SDK和协议聚合。"""
            data = {"id": "reply", "model": "Qwen/X", "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": "部分正文"}},
            ]}
            yield ("data: " + json.dumps(data) + "\n\n").encode()
            raise httpx.ReadError("private connection detail")

    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """记录实际模型请求次数，成功响应后的断流不属于HTTP重试范围。"""
        requests.append(request)
        return httpx.Response(200, stream=InterruptedStream())

    provider = LangChainProvider("openai-chat", transport=httpx.MockTransport(handle))
    received: list[str] = []
    try:
        with pytest.raises(AiError):
            async for event in provider.stream(call("openai-chat")):
                received.append(event.content)
        assert "".join(received) == "部分正文" and len(requests) == 1
    finally:
        await provider.aclose()
