"""Shared agent transport and complete-turn validation."""

import json
from contextlib import aclosing
from typing import ClassVar

from pydantic import ValidationError

from agent_bot.agent.contracts import ModelEvent, ModelTurn, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.ai.providers.base import object_value


def arguments(value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or len(value.encode()) > 65536:
        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")

    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    try:
        return object_value(json.loads(value, object_pairs_hook=pairs))
    except (ValueError, TypeError) as error:
        raise AgentError("AGENT_MODEL_RESPONSE_INVALID") from error


def tool_call(id, name, args):
    try:
        return ToolCall(call_id=id, name=name, arguments=arguments(args))
    except (ValidationError, TypeError, ValueError) as error:
        raise AgentError("AGENT_MODEL_RESPONSE_INVALID") from error


def checked_turn(call, *, content, calls, model=None, usage=None, continuation=()):
    if (
        not isinstance(content, str)
        or len(content.encode()) > 1048576
        or len(calls) > 8
        or len({c.call_id for c in calls}) != len(calls)
        or (not calls and not content.strip())
        or (call.final_only and calls)
    ):
        raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
    try:
        kwargs = {"usage": usage} if usage is not None else {}
        return ModelTurn(
            content=content,
            calls=tuple(calls),
            model=model or call.resolved.model.upstream_model,
            continuation=continuation,
            **kwargs,
        )
    except ValidationError as error:
        raise AgentError("AGENT_MODEL_RESPONSE_INVALID") from error


class AgentProviderBase:
    options: ClassVar[dict[str, str]] = {}
    path = ""

    def __init__(self, transport):
        self.transport = transport

    def validate_options(self, options):
        if set(options.model_dump(exclude_none=True)) - self.options.keys():
            raise AgentError("AI_OPTION_UNSUPPORTED", status_code=422)

    def mapped_options(self, call):
        self.validate_options(call.options)
        return {self.options[k]: v for k, v in call.options.model_dump(exclude_none=True).items()}

    def request(self, call, stream):
        headers = {"Content-Type": "application/json"}
        if call.credential is not None:
            headers["Authorization"] = f"Bearer {call.credential.get_secret_value()}"
        return {
            "payload": self.payload(call, stream),
            "headers": headers,
            "timeout_seconds": call.timeout_seconds,
        }

    async def complete_turn(self, call):
        value = await self.transport.read_json(
            call.resolved.provider.base_url + self.path, **self.request(call, False)
        )
        try:
            return self.result(object_value(value), call)
        except (KeyError, TypeError, ValueError, IndexError) as error:
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID") from error

    async def stream_turn(self, call):
        total = 0
        async with aclosing(
            self.transport.iter_bytes(
                call.resolved.provider.base_url + self.path, **self.request(call, True)
            )
        ) as raw:

            async def bounded():
                nonlocal total
                async for chunk in raw:
                    total += len(chunk)
                    if total > 4 * 1048576:
                        raise AgentError("AGENT_MODEL_RESPONSE_TOO_LARGE")
                    yield chunk

            async with aclosing(bounded()) as chunks:
                try:
                    async with aclosing(self.parse(chunks, call)) as events:
                        async for event in events:
                            yield event
                except (KeyError, TypeError, ValueError, IndexError) as error:
                    raise AgentError("AGENT_MODEL_RESPONSE_INVALID") from error

    async def aclose(self):
        await self.transport.aclose()

    def delta(self, value):
        if not isinstance(value, str):
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
        return ModelEvent(kind="text_delta", delta=value)
