"""Bounded, short-lived authorization contexts for chat-owned MCP tools."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from agent_bot.services.chat_tools import ChatToolContext


class ToolSessionError(RuntimeError):
    """Base error for tool authorization sessions."""


class ToolSessionExpiredError(ToolSessionError):
    """The supplied opaque token is unknown, expired, or revoked."""


class ToolSessionCapacityError(ToolSessionError):
    """The bounded in-memory session store is full."""


@dataclass(frozen=True, slots=True)
class _SessionEntry:
    context: ChatToolContext
    expires_at: float


class ToolSessionStore:
    """Stores only token digests and revokes every context after its AI request."""

    def __init__(
        self,
        *,
        ttl_seconds: float,
        max_sessions: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or ttl_seconds > 300:
            raise ValueError("tool session TTL must be between 1 and 300 seconds")
        if max_sessions <= 0 or max_sessions > 10_000:
            raise ValueError("invalid tool session capacity")
        self._ttl_seconds = ttl_seconds
        self._max_sessions = max_sessions
        self._clock = clock
        self._entries: dict[bytes, _SessionEntry] = {}
        self._lock = asyncio.Lock()

    async def create(self, context: ChatToolContext) -> str:
        async with self._lock:
            now = self._clock()
            self._remove_expired(now)
            if len(self._entries) >= self._max_sessions:
                raise ToolSessionCapacityError("tool session capacity reached")
            while True:
                token = secrets.token_urlsafe(32)
                digest = self._digest(token)
                if digest not in self._entries:
                    break
            self._entries[digest] = _SessionEntry(
                context=context,
                expires_at=now + self._ttl_seconds,
            )
            return token

    async def resolve(self, token: str) -> ChatToolContext:
        if not token or len(token) > 256:
            raise ToolSessionExpiredError("tool session is unavailable")
        digest = self._digest(token)
        async with self._lock:
            now = self._clock()
            entry = self._entries.get(digest)
            if entry is None or entry.expires_at <= now:
                self._entries.pop(digest, None)
                raise ToolSessionExpiredError("tool session is unavailable")
            self._entries[digest] = _SessionEntry(
                context=entry.context,
                expires_at=now + self._ttl_seconds,
            )
            return entry.context

    async def revoke(self, token: str) -> None:
        if not token:
            return
        async with self._lock:
            self._entries.pop(self._digest(token), None)

    @asynccontextmanager
    async def open(self, context: ChatToolContext) -> AsyncIterator[str]:
        token = await self.create(context)
        try:
            yield token
        finally:
            await self.revoke(token)

    def _remove_expired(self, now: float) -> None:
        expired = [digest for digest, entry in self._entries.items() if entry.expires_at <= now]
        for digest in expired:
            self._entries.pop(digest, None)

    @staticmethod
    def _digest(token: str) -> bytes:
        return hashlib.sha256(token.encode("utf-8")).digest()
