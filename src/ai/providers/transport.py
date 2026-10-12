"""Bounded HTTP transport with safe categories and no redirects or automatic retries."""

import json
from collections.abc import Mapping
from contextlib import aclosing

import httpx
from pydantic import JsonValue

from agent_bot.ai.errors import AiError
from agent_bot.services.request_deadline import remaining_timeout


class ProviderTransport:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self.http = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
            trust_env=False,
        )

    async def iter_bytes(
        self, url: str, *, payload: JsonValue, headers: Mapping[str, str], timeout_seconds: float
    ):
        try:
            async with self.http.stream(
                "POST",
                url,
                json=payload,
                headers=headers,
                timeout=remaining_timeout(timeout_seconds),
            ) as response:
                status = response.status_code
                if not 200 <= status < 300:
                    if status == 429:
                        raise AiError("AI_RATE_LIMITED", status_code=429)
                    if status in {401, 403}:
                        raise AiError("AI_UPSTREAM_AUTH_REJECTED")
                    raise AiError(
                        "AI_UPSTREAM_UNAVAILABLE" if status >= 500 else "AI_UPSTREAM_REJECTED"
                    )
                async for chunk in response.aiter_bytes():
                    remaining_timeout(timeout_seconds)
                    yield chunk
        except httpx.TimeoutException as error:
            raise AiError("AI_TIMEOUT", status_code=504) from error
        except httpx.RequestError as error:
            raise AiError("AI_UPSTREAM_UNAVAILABLE", status_code=503) from error

    async def read_json(
        self,
        url: str,
        *,
        payload: JsonValue,
        headers: Mapping[str, str],
        timeout_seconds: float,
        maximum_bytes: int = 1048576,
    ) -> JsonValue:
        content = bytearray()
        async with aclosing(
            self.iter_bytes(url, payload=payload, headers=headers, timeout_seconds=timeout_seconds)
        ) as chunks:
            async for chunk in chunks:
                if len(content) + len(chunk) > maximum_bytes:
                    raise AiError("AI_RESPONSE_TOO_LARGE")
                content.extend(chunk)
        try:
            return json.loads(content)
        except (ValueError, UnicodeError) as error:
            raise AiError("AI_RESPONSE_INVALID") from error

    async def aclose(self):
        await self.http.aclose()
