"""绘图任务的归属、取消和单进程投递串行化。"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Literal, Protocol
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict

from agent_bot.services.image_task_context import ImageTaskCancelled

if TYPE_CHECKING:
    from agent_bot.services.conversation_queue import ConversationQueueStore, QueuedConversation

ImageTaskStatus = Literal["queued", "running", "delivering", "completed", "failed", "cancelled"]
UpstreamCancellation = Literal["not_requested", "pending", "cancelled", "not_active",
                               "unsupported", "failed"]
logger = logging.getLogger(__name__)


class ImageTaskError(RuntimeError):
    """可安全返回工具调用方的稳定业务错误。"""

    def __init__(self, code: str, message: str) -> None:
        """保留错误代码，避免暴露内部任务数据。"""
        super().__init__(message)
        self.code = code
        self.message = message


class ImageTaskOwner(BaseModel):
    """可信调用上下文中的任务归属。"""

    model_config = ConfigDict(frozen=True)
    bot_id: str
    session_id: str
    user_id: str


class ImageTask(BaseModel):
    """任务公开摘要，不返回提示词、服务地址或上游标识。"""

    task_id: str
    created_at: float
    status: ImageTaskStatus = "queued"
    cancellation_requested: bool = False
    upstream_cancellation: UpstreamCancellation = "not_requested"
    delivered_parts: int = 0


class ImageTaskRecord(ImageTask):
    """仅在持久化边界使用的完整任务执行元数据。"""

    owner: ImageTaskOwner
    stream_id: str
    endpoint: str | None = None
    prompt_id: str | None = None
    submitting: bool = False
    settled: bool = False
    started: bool = False

    def public(self) -> ImageTask:
        """显式裁剪全部内部字段。"""
        return ImageTask.model_validate(self.model_dump(include=set(ImageTask.model_fields)))


class ImageTaskList(BaseModel):
    """限定归属及数量的最近任务列表。"""

    tasks: list[ImageTask]


class ImageTaskCancellation(BaseModel):
    """分开呈现本地取消与上游停止确认。"""

    task: ImageTask


class ImageTaskStore(Protocol):
    """绘图任务持久化端口。"""

    async def get(self, task_id: str) -> ImageTaskRecord | None:
        """按内部标识读取记录。"""
        ...

    async def save(self, record: ImageTaskRecord) -> None:
        """保存元数据并为已结束任务设置保留期。"""
        ...

    async def recent(self, owner: ImageTaskOwner, limit: int) -> list[ImageTaskRecord]:
        """读取限定归属的最近任务。"""
        ...

    async def request_cancel(self, owner: ImageTaskOwner, task_id: str) -> ImageTaskRecord:
        """原子验证归属并保存不可覆盖的取消意图。"""
        ...

    async def adopt(self, record: ImageTaskRecord) -> None:
        """仅在记录不存在时补录升级前的持久化任务。"""
        ...


class ImageTaskManager:
    """持久化主动取消，并将取消确认与消息投递互斥。"""

    def __init__(self, store: ImageTaskStore, queue_store: ConversationQueueStore) -> None:
        """复用图片队列资源；锁仅用于既有的单进程执行模型。"""
        self.store = store
        self.queue_store = queue_store
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    def lock(self, task_id: str) -> asyncio.Lock:
        """复用活动锁，结束后自动释放无引用的锁。"""
        lock = self._locks.get(task_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[task_id] = lock
        return lock

    async def list_tasks(self, owner: ImageTaskOwner, *, limit: int = 20) -> ImageTaskList:
        """只返回同机器人、会话、用户最近最多二十项任务。"""
        records = await self.store.recent(owner, max(1, min(limit, 20)))
        return ImageTaskList(tasks=[record.public() for record in records])

    async def cancel(self, owner: ImageTaskOwner, task_id: str) -> ImageTaskCancellation:
        """先持久化取消意图，再尝试定点取消原上游任务。"""
        requested = await self.store.request_cancel(owner, task_id)
        if not requested.cancellation_requested:
            return ImageTaskCancellation(task=requested.public())
        async with self.lock(task_id):
            record = await self.store.get(task_id)
            if record is None:
                return ImageTaskCancellation(task=requested.public())
            if record.prompt_id is not None:
                await self._cancel_upstream(record)
            if not record.started:
                record.settled = True
                await self.store.save(record)
                await self.queue_store.complete(record.stream_id)
            return ImageTaskCancellation(task=record.public())

    async def _cancel_upstream(self, record: ImageTaskRecord) -> None:
        """使用提交时固定地址；不支持时绝不调用全局中断。"""
        from agent_bot.integrations.comfy import ComfyClient

        if record.upstream_cancellation in {"cancelled", "not_active", "unsupported"}:
            return
        if record.endpoint is None or record.prompt_id is None:
            return
        client = ComfyClient(base_url=record.endpoint, timeout_seconds=10)
        try:
            result = await client.cancel(record.prompt_id)
            record.upstream_cancellation = result.status
        except Exception as error:
            record.upstream_cancellation = "failed"
            logger.warning(
                "绘图上游取消失败：task_id=%s stage=cancel error=%s",
                record.task_id, type(error).__name__,
            )
        finally:
            await client.aclose()
        await self.store.save(record)

    def execution(self, task_id: str) -> _ImageTaskExecution:
        """构造当前工作任务的窄执行上下文。"""
        return _ImageTaskExecution(self, task_id)

    async def ensure_entry(self, entry: QueuedConversation) -> None:
        """从可信队列载荷恢复旧任务，绝不覆盖已有取消标记。"""
        if await self.store.get(entry.job.job_id) is not None:
            return
        state = await self.queue_store.load_state(entry.stream_id)
        job = entry.job
        await self.store.adopt(ImageTaskRecord(
            task_id=job.job_id, created_at=job.created_at, stream_id=entry.stream_id,
            owner=ImageTaskOwner(bot_id=job.bot_id, session_id=job.session_id, user_id=job.user_id),
            status="delivering" if state.result_ready else "queued",
            started=state.result_ready, delivered_parts=state.delivery_index,
        ))


class _ImageTaskExecution:
    """工作协程可使用的取消检查、提交记录和投递锁。"""

    def __init__(self, manager: ImageTaskManager, task_id: str) -> None:
        """绑定当前持久化任务标识。"""
        self.manager = manager
        self.task_id = task_id

    async def _record(self) -> ImageTaskRecord:
        """缺失元数据时安全停止，避免未授权恢复。"""
        record = await self.manager.store.get(self.task_id)
        if record is None:
            raise ImageTaskCancelled()
        return record

    async def check_cancelled(self) -> None:
        """每个不可逆阶段之前读取持久化取消标志。"""
        if (await self._record()).cancellation_requested:
            raise ImageTaskCancelled()

    async def finished(self) -> bool:
        """已经固化终态的任务只能清理，不能再次生成或发送。"""
        record = await self._record()
        return record.settled and record.status in {"completed", "failed"}

    async def transition(self, status: ImageTaskStatus) -> None:
        """更新执行状态，已取消任务永不恢复运行。"""
        async with self.manager.lock(self.task_id):
            record = await self._record()
            if record.cancellation_requested:
                raise ImageTaskCancelled()
            record.status = status
            record.started = True
            await self.manager.store.save(record)
            if record.cancellation_requested:
                raise ImageTaskCancelled()

    async def submitting(self, endpoint: str) -> None:
        """在 HTTP 提交之前固定端点并保留提交中的不确定状态。"""
        async with self.manager.lock(self.task_id):
            record = await self._record()
            if record.cancellation_requested:
                raise ImageTaskCancelled()
            record.endpoint = endpoint
            record.prompt_id = None
            record.submitting = True
            await self.manager.store.save(record)
            if record.cancellation_requested:
                raise ImageTaskCancelled()

    async def submitted(self, prompt_id: str, endpoint: str) -> None:
        """即使已经取消也先记住上游任务，再执行定点取消。"""
        async with self.manager.lock(self.task_id):
            while True:
                try:
                    record = await self._record()
                    record.prompt_id = prompt_id
                    record.endpoint = endpoint
                    record.submitting = False
                    await self.manager.store.save(record)
                    break
                except self.manager.queue_store.transient_errors:
                    await asyncio.sleep(0.1)
            if record.cancellation_requested:
                await self.manager._cancel_upstream(record)
                raise ImageTaskCancelled()

    @asynccontextmanager
    async def delivery(self) -> AsyncIterator[None]:
        """取消返回后不再开始新的平台发送，包括等待模型的发送。"""
        async with self.manager.lock(self.task_id):
            await self.check_cancelled()
            yield

    async def delivered(self, count: int) -> None:
        """由已持投递锁的调用者保存成功发送片段数量。"""
        record = await self._record()
        record.delivered_parts = count
        await self.manager.store.save(record)

    async def finish(self, status: ImageTaskStatus) -> None:
        """执行结束才启用保留期，主动取消始终保留取消终态。"""
        async with self.manager.lock(self.task_id):
            record = await self.manager.store.get(self.task_id)
            if record is None:
                return
            record.status = "cancelled" if record.cancellation_requested else status
            record.settled = True
            await self.manager.store.save(record)
