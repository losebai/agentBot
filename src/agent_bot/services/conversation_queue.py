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

from agent_bot.ai.chat_errors import ChatAiError
from agent_bot.database_errors import DatabaseFailure
from agent_bot.media_artifacts import ImageArtifact as WorkflowImageArtifact
from agent_bot.memory_store import ChatMemoryStoreError
from agent_bot.model_guard import model_configuration_error_code
from agent_bot.models import ChatContext, ChatMediaResponse, ChatToolName, MediaOutputType
from agent_bot.services.chat.errors import ChatBusyError, is_chat_busy_error
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
    """持久化生成结果和文件投递选择，OneBot 重试不重新生成或更改发送方式。"""

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
    reply_as_file: bool = Field(default=False, alias="replyAsFile")
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

        if self.reply_as_file and (
            self.kind not in {"TEXT", "TOOL_REPLY"} or self.reply is None
            or self.delivery_parts != (self.reply.text,)
        ):
            raise ValueError("file reply must contain exactly one complete generated body")

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

    async def contains_entry(self, stream_id: str) -> bool:
        """区分仍待投递的源条目与已完成任务保留的去重回执。"""

        ...

    async def has_session_entries(self, session_id: str) -> bool:
        """重启丢失内存活动记录后，仍能识别同会话尚未领取或确认的持久任务。"""

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
    """跟踪独占状态操作和并发请求，空闲时及时回收。"""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    active: int = 0


@dataclass(frozen=True, slots=True)
class _ReservedSlot:
    """关联已准入任务的额度与会话，领取前也不能重置其历史。"""

    semaphore: asyncio.Semaphore
    session_id: str


class ConversationQueue:
    """有界预取与非等待执行准入，同会话并发但禁止运行中重置。"""

    def __init__(
        self,
        *,
        store: ConversationQueueStore,
        processor: ConversationProcessor,
        policy: QueuePolicy,
        clock: Callable[[], float] = time.time,
        image_tasks: ImageTaskManager | None = None,
    ) -> None:
        """创建单进程队列的执行槽、独占状态锁和会话活动计数。"""

        self.store = store
        self.processor = processor
        self.policy = policy
        self.image_tasks = image_tasks
        self._clock = clock
        self._semaphore = asyncio.Semaphore(policy.workers)
        self._media_semaphore = asyncio.Semaphore(policy.media_workers)
        self._prefetch_limit = (policy.workers + policy.media_workers) * 4
        self._session_slots: dict[str, _SessionSlot] = {}
        self._reserved_slots: dict[str, _ReservedSlot] = {}
        self._pending_deliveries: dict[str, str] = {}
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
        for reserved in self._reserved_slots.values():
            reserved.semaphore.release()
        self._reserved_slots.clear()
        self._pending_deliveries.clear()

    async def enqueue(self, job: ConversationJob) -> str:
        """绘图持久化排队，普通聊天仍在额度用满时立即拒绝，容量由存储统一限制。"""

        if job.ai_job_id is not None:
            # 已有模型结果只需投递；不能因模型额度满删除其持久化结果和回执。
            async with self.session_activity(job.session_id):
                stream_id = await self.store.enqueue(job)
                # 入队 I/O 期间由活动租约保护，写入成功后才登记待投递状态；
                # 无效的重复提交不能清掉另一次成功准入保留的活动记录。
                self._pending_deliveries[job.job_id] = job.session_id
                # 完成回执可能永久存在；只有仍有源条目的结果才保护待投递历史。
                if not await self.store.contains_entry(stream_id):
                    self._pending_deliveries.pop(job.job_id, None)
                return stream_id
        if not self.store.recovery_complete and not job.background_image:
            raise ChatBusyError("conversation queue is recovering")
        slot = self._session_slots.get(job.session_id)
        if slot is not None and slot.lock.locked():
            raise ChatBusyError("conversation session has an exclusive operation")
        semaphore = self._job_semaphore(job)
        if semaphore.locked() and not job.background_image:
            raise ChatBusyError("conversation concurrency limit reached")
        # 后台绘图即使工作名额已满也可排队，恢复扫描仍先处理旧条目；
        # 持久化去重回执保证重复提交不一定产生新条目。
        # 普通聊天在写入前预留额度，避免突发消息在调度器领取前形成等待队列。
        if job.background_image or self.image_tasks is not None:
            return await self.store.enqueue(job)
        if job.job_id in self._reserved_slots:
            raise ChatBusyError("conversation job is already admitted")
        await semaphore.acquire()
        self._reserved_slots[job.job_id] = _ReservedSlot(semaphore, job.session_id)
        try:
            return await self.store.enqueue(job)
        except BaseException:
            reserved = self._reserved_slots.pop(job.job_id, None)
            if reserved is not None:
                reserved.semaphore.release()
            raise

    def _job_semaphore(self, job: ConversationJob) -> asyncio.Semaphore:
        """文本与后台图片、语音分别使用独立执行额度。"""
        return self._media_semaphore if (
            job.background_image or job.media_output == "AUDIO"
        ) else self._semaphore

    @asynccontextmanager
    async def _execution_slot(self, job: ConversationJob) -> AsyncIterator[None]:
        """绘图等待可取消的执行名额，普通聊天按既有准入预留额度执行。"""
        if job.ai_job_id is not None:
            yield
            return
        reserved = self._reserved_slots.pop(job.job_id, None)
        semaphore = reserved.semaphore if reserved is not None else self._job_semaphore(job)
        if job.background_image and (execution := active_image_task.get()) is not None:
            async with execution.execution_slot(semaphore):
                yield
            return
        if reserved is None:
            if semaphore.locked() and not job.background_image:
                raise ChatBusyError("conversation concurrency limit reached")
            await semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()

    @asynccontextmanager
    async def session_activity(self, session_id: str) -> AsyncIterator[None]:
        """记录完整任务生命周期；并发生成不互斥，重置仍不能越过确认边界。"""
        slot = self._session_slots.setdefault(session_id, _SessionSlot())
        if slot.lock.locked():
            raise ChatBusyError("conversation session has an exclusive operation")
        slot.users += 1
        slot.active += 1
        try:
            yield
        finally:
            slot.active -= 1
            slot.users -= 1
            if slot.users == 0:
                del self._session_slots[session_id]

    @asynccontextmanager
    async def session_guard(
        self, session_id: str, *, timeout: float | None = None, wait: bool = True,
    ) -> AsyncIterator[None]:
        """独占状态操作只在会话空闲时准入，参数保留兼容而不再排队等待。"""

        slot = self._session_slots.setdefault(session_id, _SessionSlot())
        slot.users += 1
        acquired = False
        try:
            self._require_idle_session(session_id, slot)
            # 查询期间让旧任务正常恢复；提前持有独占锁会把正在恢复的结果误入死信。
            if await self.store.has_session_entries(session_id):
                raise ConversationSessionBusyError("conversation session has pending entries")
            # I/O 返回后重新检查活动状态，再在不让出事件循环的窗口取得空闲锁。
            self._require_idle_session(session_id, slot)
            await slot.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                slot.lock.release()
            slot.users -= 1
            if slot.users == 0:
                del self._session_slots[session_id]

    def _require_idle_session(self, session_id: str, slot: _SessionSlot) -> None:
        """独占准入前统一检查执行中、已准入未领取和待投递的会话活动。"""
        pending = any(
            reserved.session_id == session_id for reserved in self._reserved_slots.values()
        ) or session_id in self._pending_deliveries.values()
        if slot.active or pending or slot.lock.locked():
            raise ConversationSessionBusyError("conversation session is busy")

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
            reserved = self._reserved_slots.pop(entry.job.job_id, None)
            if reserved is not None:
                reserved.semaphore.release()
            self._pending_deliveries.pop(entry.job.job_id, None)

    async def _process_entry(self, entry: QueuedConversation) -> None:
        """同会话并发处理，总时限覆盖生成、重试和最终确认。"""

        try:
            self._remaining_seconds(entry)
            async with self.session_activity(entry.job.session_id):
                try:
                    remaining = self._remaining_seconds(entry)
                    with request_deadline(remaining):
                        await asyncio.wait_for(self._process_with_recovery(entry), remaining)
                except (asyncio.TimeoutError, ConversationJobExpiredError):
                    await self._expire_entry(entry)
        except (asyncio.TimeoutError, ConversationJobExpiredError):
            await self._expire_entry(entry)
        except ChatBusyError as exc:
            await self._fail_and_notify(entry, await self.store.load_state(entry.stream_id), exc)

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
                await self._fail_and_notify(
                    entry, state, ConversationJobExpiredError("conversation job expired"),
                )
                return
            except self.store.transient_errors:
                await asyncio.sleep(1)

    async def _process_with_recovery(self, entry: QueuedConversation) -> None:
        """在任务总预算内恢复短暂存储故障，不持有会话执行锁。"""

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
                async with self._execution_slot(entry.job):
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
                    await self._fail_and_notify(entry, state, exc)
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

    async def _fail_and_notify(
        self,
        entry: QueuedConversation,
        state: QueueState,
        error: Exception,
    ) -> None:
        """配置拒绝清理任务；执行故障进入死信，日志仅保留稳定错误码。"""

        execution = active_image_task.get()
        if execution is not None:
            await execution.check_cancelled()

        configuration_code = model_configuration_error_code(error)
        if configuration_code is not None:
            if execution is None:
                await self.store.complete(entry.stream_id)
            logger.error(
                "对话任务配置错误：job_id=%s code=%s，已终止且未进入死信",
                entry.job.job_id, configuration_code,
            )
        else:
            if execution is None:
                await self.store.dead_letter(
                    entry, reason=type(error).__name__, attempt=state.attempt,
                )
            logger.warning(
                "对话队列任务失败并进入死信：job_id=%s attempt=%s error=%s "
                "code=%s database=%s",
                entry.job.job_id, state.attempt, type(error).__name__,
                error.error_code if isinstance(error, ChatAiError) else None,
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
                if model_configuration_error_code(error) is not None:
                    await self.store.complete(entry.stream_id)
                else:
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

        if is_chat_busy_error(exc):
            return False
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
