"""请求总预算不因嵌套调用或流式进度被重置。"""

import asyncio
import json

import httpx
import pytest

from agent_bot.integrations.dispatch_ai import DispatchAiClient, DispatchAiUnavailableError
from agent_bot.models import ChatMessage, DispatchAiRequest
from agent_bot.services.request_deadline import (
    clear_request_deadline,
    remaining_timeout,
    request_deadline,
)


def test_nested_budget_cannot_extend_parent_and_clear_restores_it(monkeypatch) -> None:
    """嵌套请求只能缩短预算，后台任务可显式脱离继承的预算。"""
    clock = [100.0]
    monkeypatch.setattr("agent_bot.services.request_deadline.monotonic", lambda: clock[0])
    assert remaining_timeout(30) == 30
    with request_deadline(5):
        clock[0] += 2
        with request_deadline(60):
            assert remaining_timeout(30) == 3
        with clear_request_deadline():
            assert remaining_timeout(30) == 30
        assert remaining_timeout(30) == 3
        clock[0] += 4
        with pytest.raises(asyncio.TimeoutError):
            remaining_timeout(30)
    assert remaining_timeout(30) == 30


@pytest.mark.parametrize("value", [0, -1, 300001])
def test_request_budget_rejects_invalid_milliseconds(value: int) -> None:
    """协议不允许调用方超过服务端硬上限。"""
    with pytest.raises(ValueError):
        DispatchAiRequest(
            prompt="hello", sessionId="s", model="m", requestsId="r",
            messages=[ChatMessage(role="user", content="hello")], requestTimeoutMillis=value,
        )


@pytest.mark.asyncio
async def test_whole_http_call_expires_and_cancels_inflight_transport() -> None:
    """MockTransport 不执行 httpx 空闲超时，仍受客户端总超时控制。"""
    cancelled = asyncio.Event()
    async def handler(request: httpx.Request) -> httpx.Response:
        """捕获发往服务端的预算，等待取消。"""
        assert 1 <= json.loads(request.content)["requestTimeoutMillis"] <= 25
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    client = DispatchAiClient(base_url="http://ai.test", timeout_seconds=10,
                              transport=httpx.MockTransport(handler))
    try:
        with request_deadline(0.025), pytest.raises(DispatchAiUnavailableError):
            await client.complete(model="m", session_id="s", prompt="hello",
                                  messages=[ChatMessage(role="user", content="hello")],
                                  request_id="r", use_agent=False)
        assert cancelled.is_set()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_image_fetch_is_inside_total_budget(monkeypatch) -> None:
    """图片下载超时不能绕过 complete 的总预算。"""
    cancelled = asyncio.Event()
    async def fetch(*args):
        """模拟始终没有完成的图片下载。"""
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    monkeypatch.setattr("agent_bot.integrations.dispatch_ai.fetch_image_data_urls", fetch)
    client = DispatchAiClient(base_url="http://ai.test", timeout_seconds=0.025)
    try:
        with pytest.raises(DispatchAiUnavailableError):
            await client.complete(model="m", session_id="s", prompt="hello", messages=[],
                                  request_id="r", image_urls=["https://example.test/a.png"])
        assert cancelled.is_set()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_stream_progress_does_not_refresh_total_budget() -> None:
    """SSE 不断返回数据时仍会在总预算结束后关闭响应。"""
    closed = asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        """持续返回非终态事件，不访问外部网络。"""

        async def __aiter__(self):
            """在每个空闲超时之前持续发送数据。"""
            while True:
                yield b'data: {"choices":[{"index":0,"delta":{"type":"reason"}}]}\n\n'
                await asyncio.sleep(0.005)

        async def aclose(self) -> None:
            """记录 HTTP 响应被主动关闭。"""
            closed.set()

    client = DispatchAiClient(
        base_url="http://ai.test", timeout_seconds=0.04,
        transport=httpx.MockTransport(lambda _: httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()
        )),
    )
    try:
        with pytest.raises(DispatchAiUnavailableError):
            await client.complete(model="m", session_id="s", prompt="hello",
                                  messages=[ChatMessage(role="user", content="hello")],
                                  request_id="r")
        assert closed.is_set()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_media_generation_uses_remaining_budget_and_cancels() -> None:
    """媒体端点接收毫秒级剩余预算，总超时会取消响应读取。"""
    cancelled = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        """检查媒体协议携带的服务端硬上限。"""
        payload = json.loads(request.content)
        assert 1 <= payload["requestTimeoutMillis"] <= 25
        assert payload["timeoutSeconds"] == 60
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
        raise AssertionError("unreachable")

    client = DispatchAiClient(base_url="http://ai.test", timeout_seconds=10,
                              transport=httpx.MockTransport(handler))
    try:
        with request_deadline(0.025), pytest.raises(DispatchAiUnavailableError):
            await client.generate_media(
                model="image", session_id="s", prompt="cat", messages=(), request_id="r",
                output_type="IMAGE", timeout_seconds=60, max_bytes=1024, image_format="png",
                voice=None, audio_format=None, user="tester",
            )
        assert cancelled.is_set()
    finally:
        await client.aclose()
