"""绘图执行过程与持久化队列管理之间的窄上下文契约。"""

from contextvars import ContextVar
from typing import Protocol


class ImageTaskCancelled(Exception):
    """用户主动取消任务；队列应确认清理，不重试或发送失败提示。"""


class ImageTaskExecution(Protocol):
    """跟踪一次绘图执行，独立于具体 Comfy HTTP 客户端。"""

    async def check_cancelled(self) -> None:
        """确认任务仍可执行，否则抛出 ImageTaskCancelled。"""
        ...

    async def submitting(self, endpoint: str) -> None:
        """提交前记录固定服务地址，检查持久化取消意图。"""
        ...

    async def submitted(self, prompt_id: str, endpoint: str) -> None:
        """记录上游任务 ID；若已取消，则尝试取消上游后停止处理。"""
        ...


active_image_task: ContextVar[ImageTaskExecution | None] = ContextVar(
    "active_image_task", default=None,
)
