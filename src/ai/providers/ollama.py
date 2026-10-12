"""Ollama native /api/chat text and newline-delimited JSON adapter."""

from contextlib import aclosing
from typing import ClassVar

from agent_bot.ai.contracts import ProviderCall, ProviderDelta, ProviderResult, TokenUsage
from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.base import (
    BaseProvider,
    json_object,
    model_name,
    object_value,
    require_text,
)
from agent_bot.ai.providers.parsing import parse_ndjson


class OllamaProvider(BaseProvider):
    path = "/api/chat"
    options: ClassVar[dict[str, str]] = {
        "max_tokens": "num_predict",
        "temperature": "temperature",
        "top_p": "top_p",
        "top_k": "top_k",
        "min_p": "min_p",
        "repetition_penalty": "repeat_penalty",
        "stop": "stop",
        "enable_thinking": "think",
    }

    def payload(self, call: ProviderCall, stream: bool):
        options = self.mapped_options(call)
        thinking = options.pop("think", None)
        payload = {
            "model": call.resolved.model.upstream_model,
            "messages": [message.model_dump() for message in call.request.messages],
            "stream": stream,
            "options": options,
        }
        if thinking is not None:
            payload["think"] = thinking
        return payload

    def part(self, value: dict):
        if "error" in value:
            raise AiError("AI_UPSTREAM_REJECTED")
        message = object_value(value.get("message"))
        if message.get("tool_calls") or not isinstance(message.get("content", ""), str):
            raise AiError("AI_RESPONSE_INVALID")
        return message.get("content", "")

    def stats(self, value: dict):
        prompt, completion = value.get("prompt_eval_count"), value.get("eval_count")
        try:
            return TokenUsage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=prompt + completion
                if isinstance(prompt, int) and isinstance(completion, int)
                else None,
            )
        except (ValueError, TypeError) as error:
            raise AiError("AI_RESPONSE_INVALID") from error

    async def complete(self, call: ProviderCall):
        value = await self.read(call, self.payload(call, False))
        if value.get("done") is not True:
            raise AiError("AI_RESPONSE_INVALID")
        return ProviderResult(
            content=require_text(self.part(value)),
            model=model_name(value.get("model"), call),
            usage=self.stats(value),
            finish_reason=value.get("done_reason", "stop"),
        )

    async def stream(self, call: ProviderCall):
        async with aclosing(self.bytes(call, self.payload(call, True))) as chunks:
            async for data in parse_ndjson(chunks):
                value = json_object(data)
                content = self.part(value)
                if content:
                    yield ProviderDelta(kind="content", content=content)
                if value.get("done") is True:
                    yield ProviderDelta(
                        kind="done",
                        model=model_name(value.get("model"), call),
                        usage=self.stats(value),
                        finish_reason=value.get("done_reason", "stop"),
                    )
                    return
        raise AiError("AI_RESPONSE_INVALID")
