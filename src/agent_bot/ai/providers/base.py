"""保留模型发现与媒体共用的校验和文本供应商接口。"""

import json
from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import ValidationError

from agent_bot.ai.contracts import ProviderCall, ProviderDelta, ProviderResult, TokenUsage
from agent_bot.ai.errors import AiError
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
