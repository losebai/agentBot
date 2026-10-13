"""Explicit Chat Completions compatible protocol, distinct from Responses."""

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


class ChatCompletionsProvider(BaseProvider):
    path = "/chat/completions"
    options: ClassVar[dict[str, str]] = {
        "max_tokens": "max_completion_tokens",
        "temperature": "temperature",
        "top_p": "top_p",
        "presence_penalty": "presence_penalty",
        "frequency_penalty": "frequency_penalty",
        "stop": "stop",
    }

    def payload(self, call: ProviderCall, stream: bool):
        return {
            "model": call.resolved.model.upstream_model,
            "messages": [message.model_dump() for message in call.request.messages],
            "stream": stream,
            **self.mapped_options(call),
        }

    async def complete(self, call: ProviderCall):
        value = await self.read(call, self.payload(call, False))
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise AiError("AI_RESPONSE_INVALID")
        choice = object_value(choices[0])
        message = object_value(choice.get("message"))
        if (
            message.get("tool_calls")
            or message.get("function_call")
            or choice.get("finish_reason") != "stop"
        ):
            raise AiError("AI_RESPONSE_INVALID")
        return ProviderResult(
            content=require_text(message.get("content")),
            model=model_name(value.get("model"), call),
            usage=usage(value.get("usage")),
        )

    async def stream(self, call: ProviderCall):
        finished, final_model, final_usage = False, None, usage(None)
        async with aclosing(self.bytes(call, self.payload(call, True))) as chunks:
            async for data in parse_sse(chunks):
                if data == "[DONE]":
                    if not finished:
                        raise AiError("AI_RESPONSE_INVALID")
                    yield ProviderDelta(kind="done", model=final_model, usage=final_usage)
                    return
                value = json_object(data)
                final_model = model_name(value.get("model"), call)
                if value.get("usage") is not None:
                    final_usage = usage(value["usage"])
                choices = value.get("choices")
                if not isinstance(choices, list) or len(choices) > 1:
                    raise AiError("AI_RESPONSE_INVALID")
                for item in choices:
                    choice = object_value(item)
                    delta = object_value(choice.get("delta", {}))
                    if delta.get("tool_calls") or delta.get("function_call"):
                        raise AiError("AI_RESPONSE_INVALID")
                    content = delta.get("content")
                    if content is not None:
                        if not isinstance(content, str) or finished:
                            raise AiError("AI_RESPONSE_INVALID")
                        if content:
                            yield ProviderDelta(kind="content", content=content)
                    if choice.get("finish_reason") is not None:
                        if choice["finish_reason"] != "stop":
                            raise AiError("AI_RESPONSE_INVALID")
                        finished = True
        raise AiError("AI_RESPONSE_INVALID")
