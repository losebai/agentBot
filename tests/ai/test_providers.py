import json

import httpx
import pytest

from agent_bot.ai.contracts import (
    ModelView,
    ProviderCall,
    ProviderView,
    ResolvedModel,
    TextRequest,
)
from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.chat_completions import ChatCompletionsProvider
from agent_bot.ai.providers.ollama import OllamaProvider
from agent_bot.ai.providers.responses import ResponsesProvider
from agent_bot.ai.providers.transport import ProviderTransport
from agent_bot.models import LlmRequestOptions

PROVIDERS = {
    "openai-responses": ResponsesProvider,
    "openai-chat": ChatCompletionsProvider,
    "ollama": OllamaProvider,
}


def call(protocol, options=None):
    return ProviderCall(
        request=TextRequest(
            model="local",
            session_id="s",
            request_id="r",
            prompt="hi",
            options=options or LlmRequestOptions(max_tokens=123),
        ),
        resolved=ResolvedModel(
            model=ModelView(
                id=1,
                revision=1,
                alias="local",
                upstream_model="Qwen/X",
                provider_id=1,
                enabled=True,
            ),
            provider=ProviderView(
                id=1,
                revision=1,
                name="test",
                protocol=protocol,
                base_url="https://offline.invalid/v1",
                enabled=True,
            ),
        ),
    )


def response(protocol):
    if protocol == "openai-responses":
        return {
            "status": "completed",
            "model": "Qwen/X",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "你好\n\n世界"}],
                }
            ],
        }
    if protocol == "openai-chat":
        return {
            "model": "Qwen/X",
            "choices": [
                {
                    "message": {"role": "assistant", "content": "你好\n\n世界"},
                    "finish_reason": "stop",
                }
            ],
        }
    return {
        "model": "Qwen/X",
        "message": {"role": "assistant", "content": "你好\n\n世界"},
        "done": True,
    }


@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_payload_and_sync_text(protocol):
    def handle(request):
        payload = json.loads(request.content)
        assert payload["model"] == "Qwen/X"
        assert payload["stream"] is False
        if protocol == "openai-responses":
            assert request.url.path == "/v1/responses"
            assert payload["max_output_tokens"] == 123 and payload["store"] is False
        elif protocol == "openai-chat":
            assert request.url.path == "/v1/chat/completions"
            assert payload["max_completion_tokens"] == 123
        else:
            assert request.url.path == "/v1/api/chat"
            assert payload["options"]["num_predict"] == 123
        return httpx.Response(200, json=response(protocol))

    provider = PROVIDERS[protocol](ProviderTransport(httpx.MockTransport(handle)))
    try:
        result = await provider.complete(call(protocol))
        assert result.content == "你好\n\n世界"
        assert result.usage.total_tokens is None
    finally:
        await provider.aclose()


def stream_body(protocol):
    if protocol == "openai-responses":
        values = [
            {"type": "response.output_text.delta", "delta": "你"},
            {"type": "response.output_text.delta", "delta": "好"},
            {"type": "response.completed", "response": {
                "status": "completed", "model": "Qwen/X", "output": [{
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": "你好"}],
                }],
            }},
        ]
    elif protocol == "openai-chat":
        values = [
            {"model": "Qwen/X", "choices": [{"delta": {"content": "你"}}]},
            {"model": "Qwen/X", "choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]},
        ]
    else:
        values = [
            {"model": "Qwen/X", "message": {"role": "assistant", "content": "你"}, "done": False},
            {"model": "Qwen/X", "message": {"role": "assistant", "content": "好"}, "done": True},
        ]
    if protocol == "ollama":
        return "\n".join(json.dumps(value, ensure_ascii=False) for value in values).encode()
    return (
        "".join("data: " + json.dumps(value, ensure_ascii=False) + "\n\n" for value in values)
        + ("data: [DONE]\n\n" if protocol == "openai-chat" else "")
    ).encode()


class ByteStream(httpx.AsyncByteStream):
    closed = False

    async def __aiter__(self):
        for byte in self.raw:
            yield bytes([byte])

    def __init__(self, raw):
        self.raw = raw

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_incremental_stream_and_resource_close(protocol):
    stream = ByteStream(stream_body(protocol))
    provider = PROVIDERS[protocol](
        ProviderTransport(httpx.MockTransport(lambda req: httpx.Response(200, stream=stream)))
    )
    try:
        events = [event async for event in provider.stream(call(protocol))]
        assert [event.content for event in events if event.kind == "content"] == ["你", "好"]
        assert len([event for event in events if event.kind == "done"]) == 1
        assert stream.closed
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_missing_terminal_is_failure(protocol):
    raw = (
        b'{"message":{"content":"partial"},"done":false}\n'
        if protocol == "ollama"
        else b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
    )
    provider = PROVIDERS[protocol](
        ProviderTransport(httpx.MockTransport(lambda req: httpx.Response(200, content=raw)))
    )
    try:
        with pytest.raises(AiError):
            _ = [event async for event in provider.stream(call(protocol))]
    finally:
        await provider.aclose()


@pytest.mark.parametrize("protocol", ["openai-responses", "openai-chat"])
def test_unsupported_thinking_is_not_silently_ignored(protocol):
    provider = PROVIDERS[protocol](ProviderTransport())
    with pytest.raises(AiError, match="AI_OPTION_UNSUPPORTED"):
        provider.validate_options(LlmRequestOptions(enable_thinking=False))


async def test_transport_redaction_and_bounds():
    transport = ProviderTransport(
        httpx.MockTransport(lambda req: httpx.Response(429, text="private key"))
    )
    try:
        with pytest.raises(AiError) as error:
            await transport.read_json(
                "https://offline.invalid", payload={}, headers={}, timeout_seconds=1
            )
        assert error.value.code == "AI_RATE_LIMITED" and "private key" not in str(error.value)
    finally:
        await transport.aclose()
