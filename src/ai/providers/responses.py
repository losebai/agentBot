"""OpenAI Responses API text-only adapter."""

from contextlib import aclosing
from typing import ClassVar

from agent_bot.ai.contracts import ProviderCall, ProviderDelta, ProviderResult
from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.base import (
    BaseProvider,
    json_object,
    model_name,
    object_value,
    require_text,
    usage,
)
from agent_bot.ai.providers.parsing import parse_sse


class ResponsesProvider(BaseProvider):
    path = "/responses"
    options: ClassVar[dict[str, str]] = {
        "max_tokens": "max_output_tokens",
        "temperature": "temperature",
        "top_p": "top_p",
    }

    def payload(self, call: ProviderCall, stream: bool):
        return {
            "model": call.resolved.model.upstream_model,
            "input": [message.model_dump() for message in call.request.messages],
            "stream": stream,
            "store": False,
            **self.mapped_options(call),
        }

    def result(self, value: dict, call: ProviderCall):
        if value.get("status") != "completed" or not isinstance(value.get("output"), list):
            raise AiError("AI_RESPONSE_INVALID")
        texts = []
        for item in value["output"]:
            item = object_value(item)
            if item.get("type") == "reasoning":
                continue
            if item.get("type") != "message" or item.get("role") != "assistant":
                raise AiError("AI_RESPONSE_INVALID")
            for part in item.get("content", []):
                part = object_value(part)
                if part.get("type") != "output_text" or not isinstance(part.get("text"), str):
                    raise AiError("AI_RESPONSE_INVALID")
                texts.append(part["text"])
        return ProviderResult(
            content=require_text("".join(texts)),
            model=model_name(value.get("model"), call),
            usage=usage(value.get("usage"), responses=True),
        )

    async def complete(self, call: ProviderCall):
        return self.result(await self.read(call, self.payload(call, False)), call)

    async def stream(self, call: ProviderCall):
        async with aclosing(self.bytes(call, self.payload(call, True))) as chunks:
            async for data in parse_sse(chunks):
                value = json_object(data)
                kind = value.get("type")
                if kind == "response.output_text.delta":
                    if not isinstance(value.get("delta"), str):
                        raise AiError("AI_RESPONSE_INVALID")
                    yield ProviderDelta(kind="content", content=value["delta"])
                elif kind == "response.completed":
                    final = object_value(value.get("response"))
                    if final.get("status") != "completed":
                        raise AiError("AI_RESPONSE_INVALID")
                    if "output" in final:
                        self.result(final, call)
                    yield ProviderDelta(
                        kind="done",
                        model=model_name(final.get("model"), call),
                        usage=usage(final.get("usage"), responses=True),
                    )
                    return
                elif kind in {"response.failed", "response.incomplete", "error"}:
                    raise AiError("AI_UPSTREAM_REJECTED")
                elif kind == "response.output_item.added":
                    if object_value(value.get("item")).get("type") not in {"message", "reasoning"}:
                        raise AiError("AI_RESPONSE_INVALID")
        raise AiError("AI_RESPONSE_INVALID")
