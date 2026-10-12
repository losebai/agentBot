import pytest
from pydantic import ValidationError

from agent_bot.ai.contracts import ModelCreate, ProviderCreate, TextRequest
from agent_bot.models import ChatMessage


def request(**changes):
    return TextRequest(model="local", session_id="s", request_id="r", prompt="hi", **changes)


def test_text_bounds_and_last_user():
    for messages in (
        [ChatMessage(role="user", content="x")] * 101,
        [ChatMessage(role="user", content="x" * 32001)],
        [ChatMessage(role="user", content="x" * 30000)] * 5,
        [ChatMessage(role="assistant", content="x")],
    ):
        with pytest.raises(ValidationError):
            request(messages=messages)
    assert len(request(messages=[ChatMessage(role="user", content="x")] * 100).messages) == 100
    assert request(messages=[ChatMessage(role="user", content="x" * 32000)]).model == "local"


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
    assert not ProviderCreate(name="test", protocol="ollama", base_url="http://localhost").enabled
    assert (
        ModelCreate(alias="qwen", upstream_model="Qwen/X", provider_id=1).upstream_model == "Qwen/X"
    )
    with pytest.raises(ValidationError):
        ModelCreate(alias="x" * 201, upstream_model="x", provider_id=1)
