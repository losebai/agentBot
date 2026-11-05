import pytest
from pydantic import ValidationError

from agent_bot.ai.contracts import ModelCreate, ProviderCreate, TextRequest
from agent_bot.models import ChatMessage


def request(**changes):
    return TextRequest(model="local", session_id="s", request_id="r", prompt="hi", **changes)


def test_text_bounds_and_last_user():
    """请求层保留结构与单条限制，总上下文字符预算交由运行配置校验。"""
    for messages in (
        [ChatMessage(role="user", content="x")] * 101,
        [ChatMessage(role="user", content="x" * 32001)],
        [ChatMessage(role="assistant", content="x")],
    ):
        with pytest.raises(ValidationError):
            request(messages=messages)
    assert len(request(messages=[ChatMessage(role="user", content="x")] * 100).messages) == 100
    assert request(messages=[ChatMessage(role="user", content="x" * 32000)]).model == "local"
    assert len(request(messages=[ChatMessage(role="user", content="x" * 30000)] * 5).messages) == 5


@pytest.mark.parametrize(
    "url",
    [
        "ftp://host/v1",
        "https://user:pass@host/v1",
        "https://host/v1?key=secret",
        "https://host/v1#secret",
        "https://host/v1/responses",
        "http://host/api/chat",
    ],
)
def test_provider_rejects_unsafe_or_full_endpoint(url):
    with pytest.raises(ValidationError):
        ProviderCreate(name="test", protocol="openai-responses", base_url=url)


def test_defaults_and_exact_model_id():
    provider = ProviderCreate(name="test", protocol="ollama", base_url="http://localhost")
    assert not provider.enabled and provider.user_agent == ""
    assert (
        ModelCreate(alias="qwen", upstream_model="Qwen/X", provider_id=1).upstream_model == "Qwen/X"
    )
    with pytest.raises(ValidationError):
        ModelCreate(alias="x" * 201, upstream_model="x", provider_id=1)


def test_openai_provider_requires_credential_but_ollama_can_be_anonymous():
    """远程兼容接口不能匿名登记，本地 Ollama 仍允许不配置鉴权。"""
    with pytest.raises(ValidationError):
        ProviderCreate(
            name="remote", protocol="openai-chat", base_url="https://offline.invalid/v1"
        )
    assert ProviderCreate(
        name="local", protocol="ollama", base_url="http://127.0.0.1:11434"
    ).credential is None


@pytest.mark.parametrize("user_agent", ["AgentBot\nInjected", "中文客户端", "x" * 513])
def test_provider_rejects_invalid_user_agent(user_agent):
    """User-Agent 必须能被 HTTP 客户端安全编码为单一请求头。"""
    with pytest.raises(ValidationError):
        ProviderCreate(
            name="test", protocol="openai-chat", base_url="https://offline.invalid/v1",
            user_agent=user_agent,
        )
