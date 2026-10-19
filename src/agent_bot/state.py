"""会话状态、访问策略与纯文本处理。"""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from agent_bot.models import ChatMessage

REQUEST_RECEIPT_TTL_SECONDS = 86_400
_MAX_REQUEST_RECEIPTS = 100_000


def validate_request_id(request_id: str | None) -> str | None:
    """限制幂等请求标识，空值只供兼容旧调用使用。"""
    if request_id is not None and (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 128
        or request_id != request_id.strip()
        or any(ord(char) < 32 for char in request_id)
    ):
        raise ValueError("request id must contain between 1 and 128 visible characters")
    return request_id


@dataclass(frozen=True, slots=True, repr=False)
class AccessPolicy:
    """访问策略：群聊采用黑名单 (所有人可 @发言)，私聊采用白名单。"""

    allowed_users: frozenset[str] | set[str] = frozenset()
    allowed_groups: frozenset[str] | set[str] = frozenset()
    blocked_groups: frozenset[str] | set[str] = frozenset()
    blocked_users: frozenset[str] | set[str] = frozenset()
    allow_all: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_users", frozenset(map(str, self.allowed_users)))
        object.__setattr__(self, "allowed_groups", frozenset(map(str, self.allowed_groups)))
        object.__setattr__(self, "blocked_users", frozenset(map(str, self.blocked_users)))
        object.__setattr__(self, "blocked_groups", frozenset(map(str, self.blocked_groups)))

    def allows(self, *, user_id: str, group_id: str | None = None) -> bool:
        user_id = str(user_id)
        group_id = str(group_id) if group_id is not None else None

        if user_id in self.blocked_users:
            return False

        if group_id is not None:
            return group_id not in self.blocked_groups

        if self.allow_all:
            return True
        return user_id in self.allowed_users


def build_session_scope(
    *,
    bot_id: str,
    user_id: str,
    group_id: str | None = None,
    group_shared: bool = False,
) -> str:
    """构造带机器人维度的稳定会话作用域。"""

    bot = str(bot_id)
    user = str(user_id)
    if group_id is None:
        return f"bot:{bot}:private:{user}"
    group = str(group_id)
    if group_shared:
        return f"bot:{bot}:group:{group}"
    return f"bot:{bot}:group:{group}:user:{user}"


def external_session_id(scope: str, secret: str) -> str:
    """使用 HMAC 生成可持久化但不暴露 QQ 号的会话 ID。"""

    if not secret:
        raise ValueError("session secret cannot be empty")
    digest = hmac.new(secret.encode(), scope.encode(), hashlib.sha256).hexdigest()
    return f"qq_{digest[:32]}"


@dataclass(frozen=True, slots=True, repr=False)
class ConversationTurn:
    """一轮可进入近期历史或心智记忆压缩的对话。"""

    user: str
    assistant: str
    request_id: str | None = None

    @property
    def size(self) -> int:
        return len(self.user) + len(self.assistant)


@dataclass(frozen=True, slots=True, repr=False)
class MemoryCompression:
    """一次会话记忆压缩快照；revision 用于拒绝过期结果。"""

    session_id: str
    revision: int
    previous_memory: str
    turns: tuple[ConversationTurn, ...]
    generation_id: str


@dataclass(frozen=True, slots=True, repr=False)
class ConversationContext:
    """发送给模型前读取的一致会话快照。"""

    memory: str
    messages: tuple[ChatMessage, ...]
    generation_id: str | None = None


class ConversationStoreProtocol(Protocol):
    """Synchronous conversation store used behind an async service boundary."""

    def context(self, session_id: str) -> ConversationContext: ...

    @property
    def history_turns(self) -> int:
        """返回近期历史开关，出站日志遵循同一策略。"""
        ...

    @property
    def max_history_chars(self) -> int:
        """返回历史模型视图的字符预算，不影响原件保存。"""
        ...

    def ensure_generation(self, session_id: str) -> str:
        """确保当前会话存在并返回其稳定代际。"""
        ...

    def commit_exchange(
        self,
        session_id: str,
        user: str,
        assistant: str,
        *,
        request_id: str | None = None,
    ) -> MemoryCompression | None: ...

    def apply_memory(self, compression: MemoryCompression, summary: str) -> bool: ...

    def clear(self, session_id: str) -> bool: ...


@dataclass(slots=True)
class _History:
    turns: deque[ConversationTurn]
    touched_at: float
    memory: str = ""
    memory_revision: int = 0
    generation_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class ConversationStore:
    """带近期原文、滚动心智记忆、TTL 与 LRU 上限的内存会话仓库。"""

    def __init__(
        self,
        *,
        history_turns: int,
        max_sessions: int,
        ttl_seconds: float,
        max_history_chars: int = 1_000_000,
        memory_enabled: bool = False,
        max_memory_chars: int = 4000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if history_turns < 0:
            raise ValueError("history_turns cannot be negative")
        if (
            max_sessions <= 0
            or ttl_seconds <= 0
            or max_history_chars <= 0
            or max_memory_chars <= 0
        ):
            raise ValueError("store limits must be positive")
        self._history_turns = history_turns
        self._max_sessions = max_sessions
        self._ttl_seconds = ttl_seconds
        self._max_history_chars = max_history_chars
        self._memory_enabled = memory_enabled
        self._max_memory_chars = max_memory_chars
        self._clock = clock
        self._sessions: OrderedDict[str, _History] = OrderedDict()
        self._receipts: OrderedDict[tuple[str, str], float] = OrderedDict()

    @property
    def history_turns(self) -> int:
        """读取近期历史开关，独立发送的消息也遵循该设置。"""
        return self._history_turns

    @property
    def max_history_chars(self) -> int:
        """返回模型历史预算，持久素材不受此限制。"""
        return self._max_history_chars

    def context(self, session_id: str) -> ConversationContext:
        """读取当前有效历史及代际，不为单纯读取创建新会话。"""
        history = self._get_active(session_id)
        if history is None:
            return ConversationContext(memory="", messages=())
        messages: list[ChatMessage] = []
        for turn in history.turns:
            messages.extend((
                ChatMessage(role="user", content=turn.user, history_request_id=turn.request_id),
                ChatMessage(role="assistant", content=turn.assistant,
                            history_request_id=turn.request_id),
            ))
        return ConversationContext(
            memory=history.memory, messages=tuple(messages), generation_id=history.generation_id
        )

    def ensure_generation(self, session_id: str) -> str:
        """为没有普通轮次的主动发言固定代际，重置或过期后重新生成。"""
        now = self._clock()
        history = self._get_active(session_id, now=now)
        if history is None:
            history = _History(turns=deque(), touched_at=now)
            self._sessions[session_id] = history
        history.touched_at = now
        self._sessions.move_to_end(session_id)
        self._evict_expired(now)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        return history.generation_id

    def messages(self, session_id: str) -> list[ChatMessage]:
        return list(self.context(session_id).messages)

    def memory(self, session_id: str) -> str:
        return self.context(session_id).memory

    def commit_exchange(
        self,
        session_id: str,
        user: str,
        assistant: str,
        *,
        request_id: str | None = None,
    ) -> MemoryCompression | None:
        """追加已投递轮次，在任务重试窗口内按会话和请求标识去重。"""
        request_id = validate_request_id(request_id)
        if self._history_turns == 0 and not self._memory_enabled:
            return None
        now = self._clock()
        if request_id is not None:
            while self._receipts:
                key, expires_at = next(iter(self._receipts.items()))
                if expires_at > now:
                    break
                self._receipts.pop(key)
            key = (session_id, request_id)
            if key in self._receipts:
                return None
            if len(self._receipts) >= _MAX_REQUEST_RECEIPTS:
                raise RuntimeError("conversation request receipt capacity exceeded")
            self._receipts[key] = now + REQUEST_RECEIPT_TTL_SECONDS
        history = self._get_active(session_id, now=now)
        if history is None:
            history = _History(turns=deque(), touched_at=now)
            self._sessions[session_id] = history
        history.turns.append(
            ConversationTurn(user=user, assistant=assistant, request_id=request_id)
        )
        history.touched_at = now
        compression = self._trim_history(session_id, history)
        if not history.turns and not history.memory:
            self._sessions.pop(session_id, None)
            return compression
        self._sessions.move_to_end(session_id)
        self._evict_expired(now)
        while len(self._sessions) > self._max_sessions:
            self._sessions.popitem(last=False)
        return compression

    def apply_memory(self, compression: MemoryCompression, summary: str) -> bool:
        """仅把仍对应当前 revision 的非空压缩结果写回会话。"""

        history = self._get_active(compression.session_id)
        normalized = summary.strip()
        if (
            history is None
            or history.memory_revision != compression.revision
            or history.generation_id != compression.generation_id
            or not normalized
        ):
            return False
        history.memory = normalized[: self._max_memory_chars]
        return True

    def clear(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def _get_active(self, session_id: str, *, now: float | None = None) -> _History | None:
        history = self._sessions.get(session_id)
        if history is None:
            return None
        current = self._clock() if now is None else now
        if current - history.touched_at >= self._ttl_seconds:
            self._sessions.pop(session_id, None)
            return None
        self._sessions.move_to_end(session_id)
        return history

    def _trim_history(
        self,
        session_id: str,
        history: _History,
    ) -> MemoryCompression | None:
        total_chars = sum(turn.size for turn in history.turns)
        overflow = (
            len(history.turns) > self._history_turns
            or total_chars > self._max_history_chars
        )
        if not overflow:
            return None

        if not self._memory_enabled:
            while history.turns and (
                len(history.turns) > self._history_turns
                or total_chars > self._max_history_chars
            ):
                total_chars -= history.turns.popleft().size
            return None

        target_turns = 0 if self._history_turns == 0 else max(1, self._history_turns // 2)
        target_chars = max(1, self._max_history_chars // 2)
        evicted: list[ConversationTurn] = []
        while history.turns and (
            len(history.turns) > target_turns or total_chars > target_chars
        ):
            turn = history.turns.popleft()
            total_chars -= turn.size
            evicted.append(turn)

        history.memory_revision += 1
        compression = MemoryCompression(
            session_id=session_id,
            revision=history.memory_revision,
            previous_memory=history.memory,
            turns=tuple(evicted),
            generation_id=history.generation_id,
        )
        history.memory = _fallback_memory(compression, self._max_memory_chars)
        return compression

    def _evict_expired(self, now: float) -> None:
        expired = [
            session_id
            for session_id, history in self._sessions.items()
            if now - history.touched_at >= self._ttl_seconds
        ]
        for session_id in expired:
            self._sessions.pop(session_id, None)


def _fallback_memory(compression: MemoryCompression, max_chars: int) -> str:
    """模型压缩不可用时保留一个有界、偏向较新内容的本地摘要。"""

    sections: list[str] = []
    if compression.previous_memory:
        sections.append(f"已有记忆：\n{compression.previous_memory}")
    sections.extend(
        f"用户：{turn.user}\n助手：{turn.assistant}" for turn in compression.turns
    )
    combined = "\n\n".join(sections).strip()
    return combined if len(combined) <= max_chars else combined[-max_chars:]


def split_reply(text: str, *, chunk_chars: int, max_reply_chars: int) -> list[str]:
    """限制总长度，并优先按换行或中文标点切分纯文本回复。"""

    if chunk_chars <= 0 or max_reply_chars <= 0:
        raise ValueError("reply limits must be positive")
    if not text:
        return []

    marker = "[内容过长，已截断]"
    if len(text) > max_reply_chars:
        if max_reply_chars <= len(marker):
            text = marker[:max_reply_chars]
        else:
            text = text[: max_reply_chars - len(marker)] + marker

    chunks: list[str] = []
    start = 0
    boundaries = "\n。！？；;!?"
    while start < len(text):
        hard_end = min(start + chunk_chars, len(text))
        end = hard_end
        if hard_end < len(text):
            minimum = start + max(1, chunk_chars // 2)
            candidates = [text.rfind(char, minimum, hard_end + 1) for char in boundaries]
            boundary = max(candidates)
            if boundary >= minimum:
                end = boundary + 1
        chunks.append(text[start:end])
        start = end
    return chunks


class MessageCounter:
    """限制用户在指定时间窗口内的消息条数；limit 为 0 表示不限制。"""

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit < 0:
            raise ValueError("limit cannot be negative")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        self._limit = limit
        self._window_seconds = window_seconds
        self._clock = clock
        self._timestamps: dict[str, deque[float]] = {}

    @property
    def limit(self) -> int:
        return self._limit

    def remaining(self, user_id: str, *, now: float | None = None) -> int:
        """返回当前窗口内的剩余消息条数。"""

        if self._limit == 0:
            return -1
        now = now if now is not None else self._clock()
        active = self._active_timestamps(user_id, now=now)
        return max(0, self._limit - len(active))

    def try_consume(self, user_id: str, *, now: float | None = None) -> bool:
        """尝试消费一条消息额度；成功返回 True，已超限返回 False。"""

        if self._limit == 0:
            return True
        user_id = str(user_id)
        now = now if now is not None else self._clock()
        active = self._active_timestamps(user_id, now=now)
        if len(active) >= self._limit:
            self._timestamps[user_id] = active
            return False
        active.append(now)
        self._timestamps[user_id] = active
        return True

    def _active_timestamps(self, user_id: str, *, now: float) -> deque[float]:
        dq = self._timestamps.get(user_id)
        if dq is None:
            return deque()
        cutoff = now - self._window_seconds
        while dq and dq[0] <= cutoff:
            dq.popleft()
        return dq
