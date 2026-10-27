"""当前群最近普通文本的有界内存上下文。"""

from __future__ import annotations

import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True, repr=False)
class RecentGroupMessage:
    """一条成员或机器人群消息；可选投递标识仅用于确认消息去重。"""

    sender_id: str
    sender_name: str | None
    content: str
    delivery_id: str | None = None

    def __post_init__(self) -> None:
        """限制持久投递标识格式，旧的成员消息不要求提供标识。"""
        if self.delivery_id is not None and (
            not isinstance(self.delivery_id, str) or len(self.delivery_id) != 64
            or any(char not in "0123456789abcdef" for char in self.delivery_id)
        ):
            raise ValueError("invalid group delivery ID")

    def render(self) -> str:
        sender = (
            f"{self.sender_name}(QQ {self.sender_id})"
            if self.sender_name
            else f"QQ {self.sender_id}"
        )
        return f"{sender}：{self.content}"


@dataclass(frozen=True, slots=True)
class RecentGroupSnapshot:
    """同一次读取的群背景与最后一条消息，避免从渲染文本猜测消息边界。"""

    context: str | None
    latest_message: RecentGroupMessage | None


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
        delivery_id: str | None = None,
    ) -> None:
        """追加群消息；已确认的出站消息按投递标识去重。"""
        ...

    def context(self, *, bot_id: str, group_id: str) -> str | None: ...

    def snapshot(self, *, bot_id: str, group_id: str) -> RecentGroupSnapshot:
        """读取同一版本的背景文本与最后一条消息。"""
        ...


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
        delivery_id: str | None = None,
    ) -> None:
        """按机器人与群追加真实消息，恢复同一已确认投递不再次追加。"""
        now = self._clock()
        self._evict_expired(now)
        message = RecentGroupMessage(
            sender_id=str(sender_id),
            sender_name=sender_name,
            content=content,
            delivery_id=delivery_id,
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
                delivery_id=message.delivery_id,
            )
        key = (str(bot_id), str(group_id))
        history = self._groups.get(key)
        if history is None or now - history.touched_at >= self._ttl_seconds:
            history = _GroupHistory(messages=deque(), touched_at=now)
            self._groups[key] = history
        messages = history.messages
        if delivery_id is not None and any(item.delivery_id == delivery_id for item in messages):
            return
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
        """保留普通聊天读取近期背景的原接口。"""
        return self.snapshot(bot_id=bot_id, group_id=group_id).context

    def snapshot(self, *, bot_id: str, group_id: str) -> RecentGroupSnapshot:
        """裁剪及过期策略作用于整个快照，最后消息不另行缓存。"""
        key = (str(bot_id), str(group_id))
        history = self._groups.get(key)
        if history is None:
            return RecentGroupSnapshot(None, None)
        if self._clock() - history.touched_at >= self._ttl_seconds:
            self._groups.pop(key)
            return RecentGroupSnapshot(None, None)
        if not history.messages:
            return RecentGroupSnapshot(None, None)
        self._groups.move_to_end(key)
        return RecentGroupSnapshot(
            "\n".join(message.render() for message in history.messages), history.messages[-1],
        )

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
