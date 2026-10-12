import json

import httpx
import pytest

from agent_bot.ai.errors import AiError
from agent_bot.ai.routing import RoutingAiClient
from agent_bot.integrations.dispatch_ai import DispatchAiClient, DispatchAiRejectedError
from agent_bot.models import ChatMessage, LlmRequestOptions

from .test_service import make_service


async def test_plain_python_and_explicit_java(catalog, ai_params, ai_database):
    calls = []

    def python(req):
        calls.append("python")
        assert json.loads(req.content)["model"] == "exact/model"
        return httpx.Response(200, json={"message": {"content": "python answer"}, "done": True})

    def java(req):
        calls.append("java")
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text='data: {"choices":[{"index":0,"delta":{"type":"result",'
            '"content":"java answer"}}]}\n\n'
            'data: {"choices":[{"index":0,"delta":{"type":"done"}}]}\n\n',
        )

    service = await make_service(catalog, ai_params, ai_database, python)
    legacy = DispatchAiClient(
        base_url="https://java.invalid", timeout_seconds=2, transport=httpx.MockTransport(java)
    )
    router = RoutingAiClient(
        legacy,
        service,
        plain_backend="python",
        timeout_seconds=2,
        default_options=LlmRequestOptions(),
        mcp_services=(),
        laya_enabled=False,
    )
    try:
        base = dict(
            model="local",
            session_id="s",
            prompt="hello",
            request_id="r",
            messages=[ChatMessage(role="user", content="hello")],
            use_web_search=False,
        )
        assert await router.complete(**base, use_agent=False) == "python answer"
        assert await router.complete(**base, use_agent=True) == "java answer"
        assert calls == ["python", "java"]
        with pytest.raises(DispatchAiRejectedError):
            await router.complete(**{**base, "model": "missing"}, use_agent=False)
        assert calls == ["python", "java"]
    finally:
        await router.aclose()


async def test_unsupported_option_never_falls_back(catalog, ai_params, ai_database):
    async def forbidden(req):
        raise AssertionError("no request should occur")

    service = await make_service(catalog, ai_params, ai_database, forbidden)
    legacy = DispatchAiClient(
        base_url="https://java.invalid", timeout_seconds=2, transport=httpx.MockTransport(forbidden)
    )
    router = RoutingAiClient(
        legacy,
        service,
        plain_backend="python",
        timeout_seconds=2,
        default_options=LlmRequestOptions(),
        mcp_services=(),
        laya_enabled=False,
    )
    try:
        with pytest.raises(AiError, match="AI_OPTION_UNSUPPORTED"):
            await service.complete(
                __import__("agent_bot.ai.contracts", fromlist=["TextRequest"]).TextRequest(
                    model="local",
                    session_id="s",
                    request_id="r",
                    prompt="hi",
                    options=LlmRequestOptions(presence_penalty=1),
                )
            )
    finally:
        await router.aclose()
