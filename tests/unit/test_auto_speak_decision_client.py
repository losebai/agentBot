"""自由发言 Laya 决策 HTTP 客户端测试。"""

import asyncio
import json

import httpx
import pytest

from agent_bot.integrations.dispatch_ai import (
    DispatchAiClient,
    DispatchAiProtocolError,
    DispatchAiRejectedError,
    DispatchAiUnavailableError,
)


@pytest.mark.asyncio
async def test_decide_auto_speak_posts_only_the_typed_fields_and_returns_data() -> None:
    """客户端只能传状态、会话和请求预算，不能下放 Laya 工具参数。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url == "http://ai.test/dispatch-ai/chat/autoSpeakDecision"
        assert set(payload) == {
            "state",
            "sessionId",
            "requestsId",
            "requestTimeoutMillis",
        }
        assert payload["state"] == "群聊上下文"
        assert payload["sessionId"] == "qq_decision_session"
        assert payload["requestsId"] == "auto-1"
        assert 1 <= payload["requestTimeoutMillis"] <= 3000
        assert request.headers["X-Request-ID"] == "auto-1"
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(
            200,
            json={
                "code": 200,
                "description": None,
                "data": {
                    "shouldSpeak": True,
                    "confidence": 0.91,
                    "model": "multilingual",
                },
            },
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        token="test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await client.decide_auto_speak(
            state="群聊上下文",
            session_id="qq_decision_session",
            request_id="auto-1",
        )
    finally:
        await client.aclose()

    assert result.should_speak is True
    assert result.confidence == 0.91
    assert result.model == "multilingual"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "payload", "error_type"),
    [
        (503, {"private": "provider body"}, DispatchAiUnavailableError),
        (429, {"private": "busy body"}, DispatchAiUnavailableError),
        (400, {"private": "validation body"}, DispatchAiRejectedError),
        (200, {"code": 409, "description": "private", "data": None}, DispatchAiRejectedError),
        (200, {"code": 200, "description": None, "data": None}, DispatchAiProtocolError),
        (
            200,
            {
                "code": 200,
                "description": None,
                "data": {
                    "shouldSpeak": "true",
                    "confidence": 0.9,
                    "model": "multilingual",
                },
            },
            DispatchAiProtocolError,
        ),
    ],
)
async def test_decide_auto_speak_classifies_http_and_protocol_failures_without_leaks(
    status: int,
    payload: dict[str, object],
    error_type: type[Exception],
) -> None:
    """上游正文只能用于类型校验，不能出现在调用方可见异常中。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload)

    client = DispatchAiClient(
        base_url="http://ai.test",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(error_type) as captured:
            await client.decide_auto_speak(
                state="上下文",
                session_id="qq_decision_session",
                request_id="auto-error",
            )
    finally:
        await client.aclose()

    assert "private" not in str(captured.value)
    assert "provider body" not in str(captured.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("network_error", [httpx.ReadTimeout("slow"), httpx.ConnectError("down")])
async def test_decide_auto_speak_maps_network_failures_to_unavailable(
    network_error: httpx.RequestError,
) -> None:
    """网络和 HTTP 超时统一成为可安全分类的不可用错误。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        raise network_error

    client = DispatchAiClient(
        base_url="http://ai.test",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DispatchAiUnavailableError, match=r"unavailable|timed out"):
            await client.decide_auto_speak(
                state="上下文",
                session_id="qq_decision_session",
                request_id="auto-network",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_decide_auto_speak_enforces_the_shared_total_deadline() -> None:
    """MockTransport 不执行自身超时也不能绕过客户端总预算。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={})

    client = DispatchAiClient(
        base_url="http://ai.test",
        timeout_seconds=0.01,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DispatchAiUnavailableError, match="timed out"):
            await client.decide_auto_speak(
                state="上下文",
                session_id="qq_decision_session",
                request_id="auto-timeout",
            )
    finally:
        await client.aclose()
