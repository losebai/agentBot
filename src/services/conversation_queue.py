"""自然聊天持久化队列的契约、模型与调度编排。"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from agent_bot.database_errors import DatabaseFailure
from agent_bot.media_artifacts import ImageArtifact as WorkflowImageArtifact
from agent_bot.memory_store import ChatMemoryStoreError
from agent_bot.models import ChatContext, ChatMediaResponse, ChatToolName, MediaOutputType
from agent_bot.services.image_task_context import ImageTaskCancelled, active_image_task
from agent_bot.services.image_tasks import ImageTaskManager
from agent_bot.services.request_deadline import remaining_timeout, request_deadline
from agent_bot.services.workflow_execution import WorkflowImageRequest

logger = logging.getLogger(__name__)

_QQ_ID_PATTERN = r"^[1-9][0-9]{4,19}$"
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"


class ConversationQueueError(RuntimeError):
    """Persistent conversation queue error."""


class ConversationQueueFullError(ConversationQueueError):
    """有界持久化队列已经达到容量上限。"""


class ConversationSessionBusyError(ConversationQueueError):
    """会话仍有任务执行，不能在运行时租约内等待执行重置。"""


class ConversationJobExpiredError(ConversationQueueError):
    """The queued request exceeded its allowed processing lifetime."""


class ConversationEntryDiscardedError(ConversationQueueError):
    """存储边界已隔离损坏条目，编排器不得通知或重试。"""


class PersistedGeneratedReply(BaseModel):
    """持久化模型回复及其请求来源。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    text: str = Field(min_length=1, max_length=1_000_000)
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    source: Literal["LLM"] = "LLM"


class ConversationJob(BaseModel):
    """Serializable natural conversation request without runtime credentials."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    version: Literal[2] = 2
    job_id: str = Field(alias="jobId", pattern=_IDENTIFIER_PATTERN)
    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    created_at: float = Field(alias="createdAt", ge=0, allow_inf_nan=False)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=255)
    bot_id: str = Field(alias="botId", pattern=_QQ_ID_PATTERN)
    scene: Literal["private", "group"]
    group_id: str | None = Field(default=None, alias="groupId", pattern=_QQ_ID_PATTERN)
    user_id: str = Field(alias="userId", pattern=_QQ_ID_PATTERN)
    prompt: str = Field(min_length=1, max_length=20_000)
    user: str | None = Field(default=None, min_length=1, max_length=128)
    user_name: str | None = Field(default=None, alias="userName", max_length=128)
    chat_context: ChatContext | None = Field(default=None, alias="chatContext")
    image_urls: tuple[str, ...] = Field(default_factory=tuple, alias="imageUrls", max_length=8)
    video_urls: tuple[str, ...] = Field(default_factory=tuple, alias="videoUrls", max_length=2)
    chat_tools: tuple[ChatToolName, ...] = Field(
        default_factory=tuple,
        alias="chatTools",
    )
    media_output: MediaOutputType | None = Field(default=None, alias="mediaOutput")
    laya_decision: bool = Field(default=False, alias="layaDecision")
    ai_job_id: str | None = Field(default=None, alias="aiJobId", pattern=r"^[a-f0-9]{32}$")
    ai_job_reply: PersistedGeneratedReply | None = Field(default=None, alias="aiJobReply")
    background_image: bool = Field(default=False, alias="backgroundImage")
    workflow_request: WorkflowImageRequest | None = Field(default=None, alias="workflowRequest")
    mention_target_id: str | None = Field(
        default=None,
        alias="mentionTargetId",
        pattern=_QQ_ID_PATTERN,
    )
    recent_group_context: str | None = Field(
        default=None,
        alias="recentGroupContext",
        max_length=12_000,
    )
    auto_speak_max_reply_chars: int | None = Field(
        default=None,
        alias="autoSpeakMaxReplyChars",
        ge=1,
        le=1_000,
    )
    system_prompt_append: str | None = Field(
        default=None,
        alias="systemPromptAppend",
        min_length=1,
        max_length=1_000,
    )
    image_start_reply: PersistedGeneratedReply | None = Field(
        default=None,
        alias="imageStartReply",
    )
    image_start_parts: tuple[str, ...] | None = Field(
        default=None,
        alias="imageStartParts",
        max_length=10_000,
    )

    @field_validator("chat_tools", mode="before")
    @classmethod
    def remove_retired_reply_tool(cls, value: object) -> object:
        """旧队列任务移除已下线的发送工具，恢复后统一生成最终正文。"""
        if isinstance(value, (list, tuple)):
            return tuple(tool for tool in value if tool != "send_reply")
        return value

    @model_validator(mode="after")
    def validate_target_and_mode(self) -> ConversationJob:
        """校验目标、工具和开画分段；旧 v2 未存分段时保留单段兼容语义。"""

        if self.scene == "group" and self.group_id is None:
            raise ValueError("group conversation job requires groupId")
        if self.scene == "private" and (
            self.group_id is not None or self.mention_target_id is not None
        ):
            raise ValueError("private conversation job cannot contain group delivery fields")
        if len(set(self.chat_tools)) != len(self.chat_tools):
            raise ValueError("conversation job tools cannot contain duplicates")
        if (self.ai_job_id is None) != (self.ai_job_reply is None):
            raise ValueError("AI job result requires both identifier and generated reply")
        if self.ai_job_id is not None:
            if (
                self.job_id != f"ai-job-{self.ai_job_id}" or self.request_id != self.job_id
                or self.ai_job_reply.request_id != self.request_id
                or self.chat_tools or self.media_output or self.background_image
                or self.laya_decision or self.system_prompt_append is not None
                or len(self.ai_job_reply.text) > 64000
                or self.image_urls or self.video_urls
            ):
                raise ValueError("AI job result cannot generate new content or change its identity")
        if self.laya_decision and (self.chat_tools or self.media_output or self.background_image):
            raise ValueError("Laya decision job cannot contain other tools or media generation")
        if self.media_output is not None and (
            self.image_urls or self.video_urls or self.chat_tools
        ):
            raise ValueError("media generation job cannot contain input media or tools")
        if self.background_image:
            if (
                self.chat_tools or self.image_urls or self.video_urls or self.image_start_reply
                or self.image_start_parts
                or (self.workflow_request is None) == (self.media_output != "IMAGE")
                or (self.workflow_request is not None and self.media_output is not None)
            ):
                raise ValueError("background image job requires exactly one image source")
            if self.workflow_request is not None and self.workflow_request.input_image is not None:
                scope = self.workflow_request.input_image.subfolder.rsplit("/", 1)[-1]
                if self.job_id != f"image-{scope}":
                    raise ValueError("background image reference scope does not match job")
        elif self.workflow_request is not None:
            raise ValueError("workflow request requires a background image job")
        workflow_enabled = "local_workflow_generate_image" in self.chat_tools
        if self.image_start_reply is not None and not workflow_enabled:
            raise ValueError("image start message requires the workflow image tool")
        if self.image_start_parts is not None:
            if self.image_start_reply is None:
                if self.image_start_parts:
                    raise ValueError("image start parts require a generated reply")
            elif (
                not self.image_start_parts
                or any(not part for part in self.image_start_parts)
                or "".join(self.image_start_parts) != self.image_start_reply.text
            ):
                raise ValueError("image start parts must equal the generated reply")
        return self


class ConversationResult(BaseModel):
    """Persisted model result reused across OneBot delivery retries."""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    version: Literal[2] = 2
    kind: Literal[
        "TEXT", "MEDIA", "MIXED", "WORKFLOW_IMAGES", "TOOL_REPLY", "SILENT", "IMAGE_QUEUED",
        "TOOL_IMAGES",
    ]
    mention_target_id: str | None = Field(
        default=None, alias="mentionTargetId", pattern=_QQ_ID_PATTERN,
    )
    reply: PersistedGeneratedReply | None = None
    delivery_parts: tuple[str, ...] = Field(
        default_factory=tuple,
        alias="deliveryParts",
        max_length=10_000,
    )
    media: ChatMediaResponse | None = None
    workflow_images: tuple[WorkflowImageArtifact, ...] = Field(
        default_factory=tuple,
        alias="workflowImages",
        max_length=1,
    )
    tool_images: tuple[WorkflowImageArtifact, ...] = Field(
        default_factory=tuple, alias="toolImages", max_length=4,
    )
    workflow_delivery_version: Literal[2] = Field(
        default=2,
        alias="workflowDeliveryVersion",
    )

    @model_validator(mode="after")
    def validate_kind(self) -> ConversationResult:
        """只允许来源可验证且与持久化分段完全一致的文字结果。"""

        if sum(
            len(base64.b64decode(image.data, validate=True)) for image in self.tool_images
        ) > 10_485_760:
            raise ValueError("tool image total size is invalid")
        if self.kind == "TOOL_IMAGES" and (
            not self.tool_images or self.reply is not None or self.media is not None
            or self.delivery_parts or self.workflow_images
        ):
            raise ValueError("tool images result fields are invalid")
        if self.kind == "SILENT" and (
            self.reply is not None or self.media is not None or self.delivery_parts
            or self.workflow_images or self.mention_target_id is not None
        ):
            raise ValueError("silent result cannot contain delivery fields")
        if self.kind == "TOOL_REPLY" and (
            self.reply is None or self.media is not None or len(self.delivery_parts) != 1
        ):
            raise ValueError("tool reply must contain exactly one message")
        if self.kind == "IMAGE_QUEUED" and (
            self.reply is None or self.media is not None or self.workflow_images
            or len(self.delivery_parts) != 1
        ):
            raise ValueError("queued image result requires its delivered start reply")
        if self.mention_target_id is not None and self.kind != "TOOL_REPLY":
            raise ValueError("tool mention requires a tool reply")
        if self.kind == "TEXT" and (
            self.reply is None
            or self.media is not None
            or self.workflow_images
            or any(not part for part in self.delivery_parts)
        ):
            raise ValueError("text result fields are invalid")
        if self.kind == "MEDIA" and (
            self.media is None
            or self.reply is not None
            or self.delivery_parts
            or self.workflow_images
        ):
            raise ValueError("media result fields are invalid")
        if self.kind == "MIXED" and (
            self.media is not None
            or self.reply is None
            or not self.delivery_parts
            or not self.workflow_images
            or sum(
                len(base64.b64decode(image.data, validate=True))
                for image in self.workflow_images
            )
            > 10_485_760
        ):
            raise ValueError("mixed result fields are invalid")
        if self.kind == "WORKFLOW_IMAGES" and (
            self.media is not None
            or self.reply is not None
            or self.delivery_parts
            or not self.workflow_images
        ):
            raise ValueError("workflow image result fields are invalid")
        if self.reply is not None and (
            not self.delivery_parts
            or any(not part for part in self.delivery_parts)
            or "".join(self.delivery_parts) != self.reply.text
        ):
            raise ValueError("delivery parts must equal the generated reply")
        return self


class QueueState(BaseModel):
    """Durable retry and delivery progress for one stream entry."""

    model_config = ConfigDict(frozen=True)

    attempt: int = Field(default=1, ge=1, le=100)
    available_at: float = Field(default=0, ge=0, allow_inf_nan=False)
    delivery_index: int = Field(default=0, ge=0, le=10_000)
    result_ready: bool = False
    image_start_delivery_index: int = Field(default=0, ge=0, le=10_000)
    image_start_delivered: bool = False


@dataclass(frozen=True, slots=True)
class QueuedConversation:
    stream_id: str
    job: ConversationJob


@dataclass(frozen=True, slots=True)
class QueuePolicy:
    workers: int
    max_attempts: int
    retry_initial_seconds: float
    retry_max_seconds: float
    job_ttl_seconds: float
    media_workers: int = 1

    def __post_init__(self) -> None:
        """拒绝不可执行的并发、重试或存活时间配置。"""

        if (
            self.workers <= 0 or self.media_workers <= 0
            or self.max_attempts <= 0 or self.job_ttl_seconds <= 0
        ):
            raise ValueError("queue policy limits must be positive")
        if (
            self.retry_initial_seconds <= 0
            or self.retry_max_seconds < self.retry_initial_seconds
        ):
            raise ValueError("queue retry policy is invalid")


class ConversationProcessor(Protocol):
    """队列任务生成、投递和提交的应用端口。"""

    @property
    def retryable_errors(self) -> tuple[type[Exception], ...]:
        """返回处理器允许按策略重试的异常类型。"""

        ...

    async def generate(self, job: ConversationJob) -> ConversationResult:
        """生成尚未投递的持久化结果。"""

        ...

    async def start_delivery_count(self, job: ConversationJob) -> int:
        """返回持久化开画文字需要投递的总片段数。"""

        ...

    async def deliver_start_part(self, job: ConversationJob, index: int) -> None:
        """在工作流执行前精确投递指定的持久化开画片段。"""

        ...

    async def delivery_count(
        self,
        job: ConversationJob,
        result: ConversationResult,
    ) -> int:
        """返回结果需要投递的总片段数。"""

        ...

    async def deliver_part(
        self,
        job: ConversationJob,
        result: ConversationResult,
        index: int,
    ) -> None:
        """投递指定索引的结果片段。"""

        ...

    async def deliver_failure(self, job: ConversationJob, error: Exception) -> None:
        """向尚未开始投递的请求发送安全失败提示。"""

        ...

    async def commit(self, job: ConversationJob, result: ConversationResult) -> None:
        """在完整投递后提交会话状态。"""

        ...


class ConversationQueueStore(Protocol):
    """持久化队列编排所需的存储端口。"""

    @property
    def transient_errors(self) -> tuple[type[Exception], ...]:
        """返回存储端口可短暂重试的异常类型。"""

        ...

    async def initialize(self) -> None:
        """初始化存储结构并验证连接。"""

        ...

    async def enqueue(self, job: ConversationJob) -> str:
        """持久化任务并返回存储标识。"""

        ...

    async def read_new(
        self,
        *,
        count: int,
        block_ms: int,
    ) -> list[QueuedConversation]:
        """读取一批尚未被消费者接收的新任务。"""

        ...

    async def reclaim(
        self,
        *,
        count: int,
        min_idle_ms: int,
    ) -> list[QueuedConversation]:
        """回收失联消费者遗留的任务。"""

        ...

    @property
    def recovery_complete(self) -> bool:
        """返回本轮启动恢复是否已扫描完全部历史 pending。"""

        ...

    async def load_state(self, stream_id: str) -> QueueState:
        """读取任务重试与投递进度。"""

        ...

    async def save_retry(
        self,
        stream_id: str,
        *,
        attempt: int,
        available_at: float,
    ) -> None:
        """保存下一次重试时间和次数。"""

        ...

    async def save_delivery_index(self, stream_id: str, delivery_index: int) -> None:
        """保存已完成的投递片段位置。"""

        ...

    async def save_image_start_delivered(self, stream_id: str) -> None:
        """保存工作流开画提示已经投递的状态。"""

        ...

    async def save_image_start_delivery_index(self, stream_id: str, index: int) -> None:
        """保存已成功投递的开画片段数量，供部分失败后恢复。"""

        ...

    async def save_result(self, stream_id: str, result: ConversationResult) -> None:
        """持久化可供投递重试复用的生成结果。"""

        ...

    async def load_result(self, stream_id: str) -> ConversationResult | None:
        """读取已经生成的任务结果。"""

        ...

    async def complete(self, stream_id: str) -> None:
        """确认并删除成功任务的持久化状态。"""

        ...

    async def dead_letter(
        self,
        entry: QueuedConversation,
        *,
        reason: str,
        attempt: int,
    ) -> None:
        """将最终失败任务移入死信区。"""

        ...


@dataclass(slots=True)
class _SessionSlot:
    """跟踪会话锁及其持有者和等待者，空闲时及时回收。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class ConversationQueue:
    """有界预取，分离文本和媒体额度，并保持同会话全生命周期顺序。"""

    def __init__(
        self,
        *,
        store: ConversationQueueStore,
        processor: ConversationProcessor,
        policy: QueuePolicy,
        clock: Callable[[], float] = time.time,
        image_tasks: ImageTaskManager | None = None,
    ) -> None:
        """创建单进程队列的执行槽、会话锁和生命周期状态。"""

        self.store = store
        self.processor = processor
        self.policy = policy
        self.image_tasks = image_tasks
        self._clock = clock
        self._semaphore = asyncio.Semaphore(policy.workers)
        self._media_semaphore = asyncio.Semaphore(policy.media_workers)
        self._prefetch_limit = (policy.workers + policy.media_workers) * 4
        self._session_slots: dict[str, _SessionSlot] = {}
        self._entry_tasks: set[asyncio.Task[None]] = set()
        self._capacity_changed = asyncio.Event()
        self._dispatcher_task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def running(self) -> bool:
        """Report dispatcher health without starting or changing a consumer."""
        return bool(self._running and self._dispatcher_task is not None
                    and not self._dispatcher_task.done())

    async def start(self) -> None:
        """初始化队列，并通过同一有界读取循环逐批恢复旧任务。"""

        if self._running:
            return
        await self.store.initialize()
        self._running = True
        self._dispatcher_task = asyncio.create_task(
            self._dispatch_loop(),
            name="chat-conversation-queue",
        )

    async def stop(self) -> None:
        """取消读取器及所有处理任务，保留 Redis pending 供下次启动恢复。"""

        self._running = False
        tasks = list(self._entry_tasks)
        if self._dispatcher_task is not None:
            tasks.append(self._dispatcher_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._dispatcher_task = None
        self._entry_tasks.clear()

    async def enqueue(self, job: ConversationJob) -> str:
        """将已验证任务写入持久化队列。"""

        return await self.store.enqueue(job)

    @asynccontextmanager
    async def session_guard(
        self, session_id: str, *, timeout: float | None = None, wait: bool = True,
    ) -> AsyncIterator[None]:
        """任务共享 FIFO 锁；已持运行时租约的重置必须选择不等待模式。"""

        slot = self._session_slots.setdefault(session_id, _SessionSlot())
        slot.users += 1
        acquired = False
        try:
            if not wait and slot.lock.locked():
                raise ConversationSessionBusyError("conversation session is busy")
            if not wait or timeout is None:
                await slot.lock.acquire()
            else:
                await asyncio.wait_for(slot.lock.acquire(), timeout)
            acquired = True
            yield
        finally:
            if acquired:
                slot.lock.release()
            slot.users -= 1
            if slot.users == 0:
                del self._session_slots[session_id]

    async def process_entry(self, entry: QueuedConversation) -> None:
        """为图片工作任务绑定可持久化取消的执行上下文。"""

        execution = self.image_tasks.execution(entry.job.job_id) if self.image_tasks else None
        token = active_image_task.set(execution)
        try:
            while True:
                try:
                    try:
                        if execution is not None:
                            await self.image_tasks.ensure_entry(entry)
                            await execution.check_cancelled()
                            if await execution.finished():
                                await self.store.complete(entry.stream_id)
                                return
                        await self._process_entry(entry)
                    except ImageTaskCancelled:
                        if execution is not None:
                            await execution.finish("cancelled")
                        await self.store.complete(entry.stream_id)
                    return
                except self.store.transient_errors:
                    await asyncio.sleep(1)
        finally:
            active_image_task.reset(token)

    async def _process_entry(self, entry: QueuedConversation) -> None:
        """等待会话和执行资源均计入总时限，重试与最终确认始终保留会话顺序。"""

        try:
            remaining = self._remaining_seconds(entry)
            async with self.session_guard(entry.job.session_id, timeout=remaining):
                try:
                    remaining = self._remaining_seconds(entry)
                    with request_deadline(remaining):
                        await asyncio.wait_for(self._process_with_recovery(entry), remaining)
                except (asyncio.TimeoutError, ConversationJobExpiredError):
                    await self._expire_entry(entry)
        except (asyncio.TimeoutError, ConversationJobExpiredError):
            await self._expire_entry(entry)

    def _remaining_seconds(self, entry: QueuedConversation) -> float:
        """按入队绝对时间计算剩余预算，重试不能刷新生命周期。"""

        remaining = entry.job.created_at + self.policy.job_ttl_seconds - self._clock()
        if remaining <= 0:
            raise ConversationJobExpiredError("conversation job expired")
        try:
            return remaining_timeout(remaining)
        except asyncio.TimeoutError as exc:
            raise ConversationJobExpiredError("conversation job expired") from exc

    async def _expire_entry(self, entry: QueuedConversation) -> None:
        """过期后只重试必要的存储清理，不再发送模型或平台请求。"""

        while True:
            try:
                state = await self.store.load_state(entry.stream_id)
                await self._dead_letter_and_notify(
                    entry, state, ConversationJobExpiredError("conversation job expired"),
                )
                return
            except self.store.transient_errors:
                await asyncio.sleep(1)

    async def _process_with_recovery(self, entry: QueuedConversation) -> None:
        """在同一会话锁及总预算内处理任务，恢复短暂 Redis 故障。"""

        while True:
            try:
                execution = active_image_task.get()
                if execution is not None:
                    await execution.check_cancelled()
                    if await execution.finished():
                        await self.store.complete(entry.stream_id)
                        return
                await self._process_attempts(entry)
                return
            except self.store.transient_errors as exc:
                logger.warning(
                    "Redis 对话任务状态暂不可用：job_id=%s error=%s",
                    entry.job.job_id, type(exc).__name__,
                )
                await asyncio.sleep(1)

    async def _process_attempts(self, entry: QueuedConversation) -> None:
        """恢复生成及逐段投递进度，重试退避时释放对应执行额度。"""

        state = await self.store.load_state(entry.stream_id)
        if state.available_at > self._clock():
            await asyncio.sleep(state.available_at - self._clock())
        while True:
            self._remaining_seconds(entry)
            try:
                execution = active_image_task.get()
                if execution is not None:
                    await execution.check_cancelled()
                    if await execution.finished():
                        await self.store.complete(entry.stream_id)
                        return
                media_job = entry.job.background_image or entry.job.media_output == "AUDIO"
                semaphore = self._media_semaphore if media_job else self._semaphore
                async with semaphore:
                    execution = active_image_task.get()
                    if execution is not None:
                        await execution.transition("running")
                    self._remaining_seconds(entry)
                    result = await self.store.load_result(entry.stream_id)
                    if (
                        entry.job.image_start_reply is not None
                        and not state.image_start_delivered
                    ):
                        start_count = await self.processor.start_delivery_count(entry.job)
                        if start_count <= 0 or state.image_start_delivery_index > start_count:
                            raise ValueError("conversation start parts are invalid")
                        for index in range(state.image_start_delivery_index, start_count):
                            self._remaining_seconds(entry)
                            if execution is None:
                                await self.processor.deliver_start_part(entry.job, index)
                            else:
                                async with execution.delivery():
                                    await self.processor.deliver_start_part(entry.job, index)
                            await self.store.save_image_start_delivery_index(
                                entry.stream_id, index + 1,
                            )
                            state = state.model_copy(
                                update={"image_start_delivery_index": index + 1},
                            )
                        await self.store.save_image_start_delivered(entry.stream_id)
                        state = QueueState(
                            attempt=state.attempt,
                            available_at=state.available_at,
                            delivery_index=state.delivery_index,
                            result_ready=state.result_ready,
                            image_start_delivery_index=state.image_start_delivery_index,
                            image_start_delivered=True,
                        )
                    if result is None:
                        if state.result_ready:
                            raise ValueError("persisted conversation result is missing")
                        self._remaining_seconds(entry)
                        if execution is not None:
                            await execution.check_cancelled()
                        result = await self.processor.generate(entry.job)
                        if execution is not None:
                            await execution.check_cancelled()
                        self._remaining_seconds(entry)
                        await self.store.save_result(entry.stream_id, result)
                        state = QueueState(
                            attempt=state.attempt,
                            available_at=state.available_at,
                            delivery_index=state.delivery_index,
                            result_ready=True,
                            image_start_delivery_index=state.image_start_delivery_index,
                            image_start_delivered=state.image_start_delivered,
                        )
                    delivery_count = await self.processor.delivery_count(entry.job, result)
                    if (
                        delivery_count < 0
                        or (delivery_count == 0) != (
                            result.kind in {"SILENT", "IMAGE_QUEUED"} and not result.tool_images
                        )
                        or state.delivery_index > delivery_count
                    ):
                        raise ValueError("conversation delivery parts are invalid")
                    for index in range(state.delivery_index, delivery_count):
                        self._remaining_seconds(entry)
                        if execution is None:
                            await self.processor.deliver_part(entry.job, result, index)
                        else:
                            await execution.transition("delivering")
                            async with execution.delivery():
                                await self.processor.deliver_part(entry.job, result, index)
                                await execution.delivered(index + 1)
                        await self.store.save_delivery_index(entry.stream_id, index + 1)
                        state = QueueState(
                            attempt=state.attempt,
                            available_at=state.available_at,
                            delivery_index=index + 1,
                            result_ready=state.result_ready,
                            image_start_delivery_index=state.image_start_delivery_index,
                            image_start_delivered=state.image_start_delivered,
                        )
                    self._remaining_seconds(entry)
                    if execution is not None:
                        await execution.check_cancelled()
                    await self.processor.commit(entry.job, result)
                    if execution is not None:
                        await execution.finish("completed")
                    await self.store.complete(entry.stream_id)
                return
            except asyncio.CancelledError:
                raise
            except ImageTaskCancelled:
                raise
            except ConversationEntryDiscardedError:
                return
            except ConversationJobExpiredError:
                raise
            except Exception as exc:
                execution = active_image_task.get()
                if execution is not None:
                    await execution.check_cancelled()
                    if await execution.finished():
                        raise
                if not self._is_retryable(exc) or state.attempt >= self.policy.max_attempts:
                    await self._dead_letter_and_notify(entry, state, exc)
                    return
                delay = min(
                    self.policy.retry_initial_seconds * (2 ** (state.attempt - 1)),
                    self.policy.retry_max_seconds,
                )
                next_attempt = state.attempt + 1
                await self.store.save_retry(
                    entry.stream_id,
                    attempt=next_attempt,
                    available_at=self._clock() + delay,
                )
                logger.warning(
                    "对话队列任务等待重试：job_id=%s attempt=%s error=%s database=%s",
                    entry.job.job_id,
                    next_attempt,
                    type(exc).__name__,
                    DatabaseFailure.from_error(exc),
                )
                await asyncio.sleep(delay)
                state = QueueState(
                    attempt=next_attempt,
                    available_at=0,
                    delivery_index=state.delivery_index,
                    result_ready=state.result_ready,
                    image_start_delivery_index=state.image_start_delivery_index,
                    image_start_delivered=state.image_start_delivered,
                )

    async def _dead_letter_and_notify(
        self,
        entry: QueuedConversation,
        state: QueueState,
        error: Exception,
    ) -> None:
        """完成失败任务，并仅为尚未开始投递的请求发送一次安全提示。"""

        execution = active_image_task.get()
        if execution is not None:
            await execution.check_cancelled()

        if execution is None:
            await self.store.dead_letter(
                entry,
                reason=type(error).__name__,
                attempt=state.attempt,
            )
        logger.warning(
            "对话队列任务失败并进入死信：job_id=%s attempt=%s error=%s database=%s",
            entry.job.job_id,
            state.attempt,
            type(error).__name__,
            DatabaseFailure.from_error(error),
        )
        if (
            isinstance(error, ConversationJobExpiredError)
            or state.result_ready or state.delivery_index > 0
        ):
            if execution is not None:
                await self._finish_failed_image(entry, state, error)
            return
        try:
            self._remaining_seconds(entry)
        except ConversationJobExpiredError:
            if execution is not None:
                await self._finish_failed_image(entry, state, error)
            return
        try:
            if execution is None:
                await self.processor.deliver_failure(entry.job, error)
            else:
                async with execution.delivery():
                    await self.processor.deliver_failure(entry.job, error)
        except asyncio.CancelledError:
            raise
        except ImageTaskCancelled:
            raise
        except Exception as notification_error:
            logger.warning(
                "对话队列失败提示投递失败：job_id=%s error=%s",
                entry.job.job_id,
                type(notification_error).__name__,
            )
        if execution is not None:
            await self._finish_failed_image(entry, state, error)

    async def _finish_failed_image(
        self, entry: QueuedConversation, state: QueueState, error: Exception,
    ) -> None:
        """失败通知结束后先持久化终态，再清理 Stream；存储重试不重新执行。"""
        execution = active_image_task.get()
        assert execution is not None
        while True:
            try:
                await execution.finish("failed")
                await execution.check_cancelled()
                await self.store.dead_letter(
                    entry, reason=type(error).__name__, attempt=state.attempt,
                )
                return
            except self.store.transient_errors:
                await asyncio.sleep(1)

    async def _dispatch_loop(self) -> None:
        """只按空余预取容量领取任务，恢复完成前不读取较新的任务。"""

        while self._running:
            self._capacity_changed.clear()
            capacity = self._prefetch_limit - len(self._entry_tasks)
            if capacity <= 0:
                await self._capacity_changed.wait()
                continue
            try:
                if not self.store.recovery_complete:
                    entries = await self.store.reclaim(count=capacity, min_idle_ms=0)
                else:
                    entries = await self.store.read_new(count=capacity, block_ms=1000)
            except asyncio.CancelledError:
                raise
            except self.store.transient_errors as exc:
                logger.warning("Redis 对话队列读取失败：error=%s", type(exc).__name__)
                await asyncio.sleep(1)
                continue
            if not entries:
                await asyncio.sleep(0.01)
                continue
            for entry in entries:
                self._schedule(entry)

    def _schedule(self, entry: QueuedConversation) -> None:
        """为已领取且在预取容量内的条目创建独立任务。"""

        task = asyncio.create_task(
            self.process_entry(entry),
            name=f"chat-entry-{entry.job.job_id}",
        )
        self._entry_tasks.add(task)
        task.add_done_callback(self._entry_finished)

    def _entry_finished(self, task: asyncio.Task[None]) -> None:
        """释放预取容量，并记录未预期故障而不暴露任务内容。"""

        self._entry_tasks.discard(task)
        self._capacity_changed.set()
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("对话队列任务异常结束：error=%s", type(error).__name__)

    def _is_retryable(self, exc: Exception) -> bool:
        """判断业务、平台或存储异常是否允许按队列策略重试。"""

        if isinstance(exc, ChatMemoryStoreError):
            failure = DatabaseFailure.from_error(exc)
            return failure is not None and failure.transient
        return isinstance(
            exc,
            (
                *self.processor.retryable_errors,
                *self.store.transient_errors,
            ),
        )
