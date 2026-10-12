"""Shared validation and typed provider boundary, with no protocol guessing."""

import json
from collections.abc import AsyncIterator
from typing import ClassVar, Protocol

from pydantic import ValidationError

from agent_bot.ai.contracts import ProviderCall, ProviderDelta, ProviderResult, TokenUsage
from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.transport import ProviderTransport
from agent_bot.models import LlmRequestOptions


class TextProvider(Protocol):
    def validate_options(self, options: LlmRequestOptions) -> None: ...
    async def complete(self, call: ProviderCall) -> ProviderResult: ...
    def stream(self, call: ProviderCall) -> AsyncIterator[ProviderDelta]: ...
    async def aclose(self) -> None: ...


def object_value(value) -> dict:
    if not isinstance(value, dict):
        raise AiError("AI_RESPONSE_INVALID")
    return value


def json_object(value: str) -> dict:
    try:
        return object_value(json.loads(value))
    except (ValueError, UnicodeError) as error:
        raise AiError("AI_RESPONSE_INVALID") from error


def require_text(value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AiError("AI_RESPONSE_INVALID")
    if len(value.encode()) > 1048576:
        raise AiError("AI_RESPONSE_TOO_LARGE")
    return value


def model_name(value, call: ProviderCall) -> str:
    model = value or call.resolved.model.upstream_model
    if not isinstance(model, str) or not model.strip() or len(model) > 255:
        raise AiError("AI_RESPONSE_INVALID")
    return model


def usage(value, *, responses: bool = False) -> TokenUsage:
    value = {} if value is None else object_value(value)
    try:
        return TokenUsage(
            prompt_tokens=value.get("input_tokens" if responses else "prompt_tokens"),
            completion_tokens=value.get("output_tokens" if responses else "completion_tokens"),
            total_tokens=value.get("total_tokens"),
        )
    except ValidationError as error:
        raise AiError("AI_RESPONSE_INVALID") from error


class BaseProvider:
    path: str
    options: ClassVar[dict[str, str]]

    def __init__(self, transport: ProviderTransport):
        self.transport = transport

    def validate_options(self, options: LlmRequestOptions) -> None:
        if set(options.model_dump(exclude_none=True)) - self.options.keys():
            raise AiError("AI_OPTION_UNSUPPORTED", status_code=422)

    def mapped_options(self, call: ProviderCall) -> dict:
        self.validate_options(call.request.options)
        return {
            self.options[name]: value
            for name, value in call.request.options.model_dump(exclude_none=True).items()
        }

    def headers(self, call: ProviderCall) -> dict[str, str]:
        result = {"Content-Type": "application/json"}
        if call.credential is not None:
            result["Authorization"] = f"Bearer {call.credential.get_secret_value()}"
        return result

    async def read(self, call: ProviderCall, payload: dict) -> dict:
        return object_value(
            await self.transport.read_json(
                call.resolved.provider.base_url + self.path,
                payload=payload,
                headers=self.headers(call),
                timeout_seconds=call.request.timeout_seconds,
            )
        )

    def bytes(self, call: ProviderCall, payload: dict):
        return self.transport.iter_bytes(
            call.resolved.provider.base_url + self.path,
            payload=payload,
            headers=self.headers(call),
            timeout_seconds=call.request.timeout_seconds,
        )

    async def aclose(self):
        await self.transport.aclose()
