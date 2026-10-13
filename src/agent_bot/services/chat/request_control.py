"""聊天非等待并发准入、会话活动统计与冷却控制。"""

from __future__ import annotations

import asyncio
import time
import weakref
from collections import OrderedDict
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from agent_bot.services.chat.errors import ChatBusyError, ChatRateLimitedError


class ChatRequestCoordinator:
    """统一管理聊天请求进入模型调用前的资源约束。"""

    def __init__(
        self,
        *,
        cooldown_seconds: float,
        max_concurrent_requests: int,
        max_rate_limit_entries: int,
        max_concurrent_media_requests: int = 1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """初始化并校验并发与冷却限制。"""

        if max_concurrent_requests <= 0 or max_concurrent_media_requests <= 0:
            raise ValueError("maximum concurrent requests must be positive")
        if cooldown_seconds < 0 or max_rate_limit_entries <= 0:
            raise ValueError("cooldown limits are invalid")
        self._cooldown_seconds = cooldown_seconds
        self._max_rate_limit_entries = max_rate_limit_entries
        self._clock = clock
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._media_semaphore = asyncio.Semaphore(max_concurrent_media_requests)
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()
        self._next_allowed: OrderedDict[str, float] = OrderedDict()
        self._active_sessions: dict[str, int] = {}

    def accept(self, session_id: str) -> None:
        """消费一次会话冷却额度，超限时抛出明确异常。"""

        if self._cooldown_seconds == 0:
            return
        now = self._clock()
        next_allowed = self._next_allowed.get(session_id, 0)
        if now < next_allowed:
            raise ChatRateLimitedError(next_allowed - now)
        self._next_allowed[session_id] = now + self._cooldown_seconds
        self._next_allowed.move_to_end(session_id)
        while len(self._next_allowed) > self._max_rate_limit_entries:
            self._next_allowed.popitem(last=False)

    @asynccontextmanager
    async def request(
        self,
        session_id: str,
        *,
        reject_busy: bool,
        consume_cooldown: bool,
        media: bool = False,
    ) -> AsyncIterator[None]:
        """同会话并发执行；所有调用在额度满或重置进行中时立即拒绝。"""

        lock = self._session_lock(session_id)
        semaphore = self._media_semaphore if media else self._semaphore
        # reject_busy 保留调用兼容性；排队入口也不能等待锁或全局执行额度。
        if lock.locked():
            raise ChatBusyError("this session is being reset")
        if semaphore.locked():
            raise ChatBusyError("global AI concurrency limit reached")
        async with semaphore:
            if consume_cooldown:
                self.accept(session_id)
            self._active_sessions[session_id] = self._active_sessions.get(session_id, 0) + 1
            try:
                yield
            finally:
                remaining = self._active_sessions[session_id] - 1
                if remaining:
                    self._active_sessions[session_id] = remaining
                else:
                    del self._active_sessions[session_id]

    async def reset(self, session_id: str, clear: Callable[[], Awaitable[bool]]) -> bool:
        """无活动请求时独占清理会话，重置期间的新请求立即拒绝。"""

        lock = self._session_lock(session_id)
        if lock.locked() or self._active_sessions.get(session_id, 0):
            raise ChatBusyError("this session already has an in-flight request")
        async with lock:
            self._next_allowed.pop(session_id, None)
            return await clear()

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        """获取或创建指定会话的弱引用互斥锁。"""

        lock = self._locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[session_id] = lock
        return lock
