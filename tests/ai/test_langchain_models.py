"""真实 LangChain 客户端通过离线 HTTP 验证项目的协议及配置边界。"""

import json
from contextlib import aclosing

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
async def test_upstream_failure_is_not_retried(protocol):
    """框架默认重试不能增加已失败请求的次数或泄漏上游错误正文。"""
    from agent_bot.ai.providers.langchain import LangChainProvider

    requests = []

    def handle(request):
        """模拟限流，检查只有一次真实传输。"""
        requests.append(request)
        return httpx.Response(429, text="private upstream secret")

    provider = LangChainProvider(protocol, transport=httpx.MockTransport(handle))
    try:
        with pytest.raises(AiError) as error:
            await provider.complete(call(protocol))
        assert error.value.code == "AI_RATE_LIMITED"
        assert "private" not in str(error.value)
        assert len(requests) == 1
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
