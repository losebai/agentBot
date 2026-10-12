"""当前群最近普通文本的有界内存上下文。"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True, repr=False)
class RecentGroupMessage:
    sender_id: str
    sender_name: str | None
    content: str

    def render(self) -> str:
        sender = (
            f"{self.sender_name}(QQ {self.sender_id})"
            if self.sender_name
            else f"QQ {self.sender_id}"
        )
        return f"{sender}：{self.content}"


@dataclass(slots=True)
class _GroupHistory:
    messages: deque[RecentGroupMessage]
    touched_at: float


class RecentGroupContextStoreProtocol(Protocol):
    """Synchronous group-context store used behind an async event boundary."""

    def record(
        self,
        *,
        bot_id: str,
        group_id: str,
        sender_id: str,
        sender_name: str | None,
        content: str,
    ) -> None: ...

    def context(self, *, bot_id: str, group_id: str) -> str | None: ...


class RecentGroupContextStore:
    """按机器人和群隔离保存最近普通文本。"""

    def __init__(
        self,
        *,
        message_limit: int,
        max_chars: int,
        ttl_seconds: float,
        max_groups: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            message_limit <= 0
            or max_chars <= 0
            or ttl_seconds <= 0
            or max_groups <= 0
        ):
            raise ValueError("recent group context limits must be positive")
        self._message_limit = message_limit
        self._max_chars = max_chars
        self._ttl_seconds = ttl_seconds
        self._max_groups = max_groups
        self._clock = clock
        self._groups: OrderedDict[tuple[str, str], _GroupHistory] = OrderedDict()

    def record(
        self,
        *,
        bot_id: str,
        group_id: str,
        sender_id: str,
        sender_name: str | None,
        content: str,
    ) -> None:
        now = self._clock()
        self._evict_expired(now)
        message = RecentGroupMessage(
            sender_id=str(sender_id),
            sender_name=sender_name,
            content=content,
        )
        rendered = message.render()
        if len(rendered) > self._max_chars:
            content_chars = self._max_chars - (len(rendered) - len(content))
            if content_chars <= 0:
                return
            message = RecentGroupMessage(
                sender_id=message.sender_id,
                sender_name=message.sender_name,
                content=message.content[:content_chars],
            )
        key = (str(bot_id), str(group_id))
        history = self._groups.get(key)
        if history is None or now - history.touched_at >= self._ttl_seconds:
            history = _GroupHistory(messages=deque(), touched_at=now)
            self._groups[key] = history
        messages = history.messages
        messages.append(message)
        while (
            len(messages) > self._message_limit
            or self._rendered_size(messages) > self._max_chars
        ):
            messages.popleft()
        history.touched_at = now
        self._groups.move_to_end(key)
        while len(self._groups) > self._max_groups:
            self._groups.popitem(last=False)

    def context(self, *, bot_id: str, group_id: str) -> str | None:
        key = (str(bot_id), str(group_id))
        history = self._groups.get(key)
        if history is None:
            return None
        if self._clock() - history.touched_at >= self._ttl_seconds:
            self._groups.pop(key)
            return None
        if not history.messages:
            return None
        self._groups.move_to_end(key)
        return "\n".join(message.render() for message in history.messages)

    def _evict_expired(self, now: float) -> None:
        expired = [
            key
            for key, history in self._groups.items()
            if now - history.touched_at >= self._ttl_seconds
        ]
        for key in expired:
            self._groups.pop(key)

    @staticmethod
    def _rendered_size(messages: deque[RecentGroupMessage]) -> int:
        return sum(len(message.render()) for message in messages) + max(
            0,
            len(messages) - 1,
        )
