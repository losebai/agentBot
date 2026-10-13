"""模型发现按供应商协议和私有快照执行，错误不泄露 Key。"""

import httpx
import pytest

from agent_bot.ai.contracts import ProviderCreate
from agent_bot.ai.errors import AiError
from agent_bot.ai.model_discovery import discover_models
from agent_bot.ai.providers.transport import ProviderTransport


@pytest.mark.parametrize(
    "protocol,path,body",
    [
        ("openai-chat", "/models", {"data": [{"id": "same/model"}]}),
        ("openai-responses", "/models", {"data": [{"id": "same/model"}]}),
        ("ollama", "/api/tags", {"models": [{"name": "same/model"}]}),
    ],
)
async def test_each_provider_uses_own_key_and_protocol(catalog, protocol, path, body):
    """即使协议和模型相同，也按当前供应商的 URL 和 Key 获取。"""
    seen = []

    def handle(request):
        """模拟模型列表，不执行生成请求。"""
        seen.append((request.method, str(request.url), request.headers["Authorization"]))
        assert not request.content
        return httpx.Response(200, json=body)

    transport = ProviderTransport(httpx.MockTransport(handle))
    try:
        for name in ("first", "second"):
            provider = await catalog.create_provider(
                ProviderCreate(
                    name=name,
                    protocol=protocol,
                    base_url=f"https://{name}.invalid/v1",
                    credential=f"{name}-key",
                )
            )
            result = await discover_models(provider, transport)
            assert result.models == ("same/model",)
            assert result.provider_id == provider.id
        assert seen == [
            ("GET", f"https://{name}.invalid/v1{path}", f"Bearer {name}-key")
            for name in ("first", "second")
        ]
    finally:
        await transport.aclose()


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"message": "private-key"}, "AI_UPSTREAM_AUTH_REJECTED"),
        (404, {}, "AI_UPSTREAM_REJECTED"),
        (429, {}, "AI_RATE_LIMITED"),
        (503, {}, "AI_UPSTREAM_UNAVAILABLE"),
        (200, {"data": [{"id": "\n"}]}, "AI_MODEL_LIST_INVALID"),
        (200, {"data": "bad"}, "AI_MODEL_LIST_INVALID"),
        (200, {"data": [{"id": "x"}] * 2001}, "AI_MODEL_LIST_INVALID"),
    ],
)
async def test_discovery_errors_are_bounded_and_do_not_expose_key(catalog, status, body, code):
    """列表接口不支持、鉴权失败或结构无效时明确失败。"""
    provider = await catalog.create_provider(
        ProviderCreate(
            name="test",
            protocol="openai-chat",
            base_url="https://test.invalid/v1",
            credential="private-key",
        )
    )
    transport = ProviderTransport(
        httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    )
    try:
        with pytest.raises(AiError) as caught:
            await discover_models(provider, transport)
        assert caught.value.code == code
        assert "private-key" not in str(caught.value)
    finally:
        await transport.aclose()


async def test_empty_list_is_a_successful_discovery(catalog):
    """有效空列表区别于获取失败。"""
    provider = await catalog.create_provider(
        ProviderCreate(
            name="empty",
            protocol="openai-chat",
            base_url="https://empty.invalid/v1",
        )
    )
    transport = ProviderTransport(
        httpx.MockTransport(lambda request: httpx.Response(200, json={"data": []}))
    )
    try:
        assert (await discover_models(provider, transport)).models == ()
    finally:
        await transport.aclose()
