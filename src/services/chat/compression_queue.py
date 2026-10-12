"""有界、按会话合并的后台记忆压缩。"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from agent_bot.state import MemoryCompression

logger = logging.getLogger(__name__)


class ChatCompressionQueue:
    """单独使用一个模型调用槽位，保留每个待处理会话的最新快照。"""

    def __init__(
        self,
        compress: Callable[[MemoryCompression], Awaitable[None]],
        *,
        capacity: int = 64,
    ) -> None:
        """初始化容量上限；仓库已保存的本地摘要不会因丢弃压缩而丢失。"""

        if not 1 <= capacity <= 1000:
            raise ValueError("compression queue capacity is invalid")
        self._compress = compress
        self._capacity = capacity
        self._pending: OrderedDict[str, MemoryCompression] = OrderedDict()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def submit(self, compression: MemoryCompression) -> bool:
        """立即提交快照，同会话合并；容量满时保留仓库已有摘要。"""

        if self._closed:
            return False
        previous = self._pending.get(compression.session_id)
        if (
            previous is not None
            and previous.generation_id == compression.generation_id
            and previous.revision >= compression.revision
        ):
            return True
        if previous is None and len(self._pending) >= self._capacity:
            logger.warning("记忆压缩队列已满，保留现有摘要")
            return False
        self._pending[compression.session_id] = compression
        if self._task is None or self._task.done():
            # 压缩拥有独立生命周期，不继承触发它的消息截止时间和请求上下文。
            self._task = contextvars.Context().run(
                asyncio.create_task, self._run(), name="dispatch-chat-memory-compression",
            )
        return True

    async def _run(self) -> None:
        """顺序处理不同会话，并隔离单次模型或数据库故障。"""

        while self._pending:
            _, compression = self._pending.popitem(last=False)
            try:
                await self._compress(compression)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.warning("后台记忆压缩失败：error=%s", type(error).__name__)

    async def aclose(self) -> None:
        """取消压缩并清空待办，确保不会继续使用已关闭的模型客户端。"""

        self._closed = True
        self._pending.clear()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
