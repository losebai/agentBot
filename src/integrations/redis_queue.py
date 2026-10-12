"""Redis 会话队列资源的创建与所有权。"""

from __future__ import annotations

import hashlib
import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel, ValidationError
from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError, WatchError

from agent_bot.bootstrap import RedisBootstrapConfig
from agent_bot.config import RuntimeConfig
from agent_bot.integrations.redis_image_tasks import RedisImageTaskStore
from agent_bot.services.conversation_queue import (
    ConversationEntryDiscardedError,
    ConversationJob,
    ConversationProcessor,
    ConversationQueue,
    ConversationQueueFullError,
    ConversationResult,
    QueuedConversation,
    QueuePolicy,
    QueueState,
)
from agent_bot.services.image_tasks import ImageTaskManager

_MAX_JOB_JSON_BYTES = 128 * 1024
_MAX_RESULT_JSON_BYTES = 16 * 1024 * 1024
_MAX_RESULT_WITH_TOOL_IMAGES_BYTES = 32 * 1024 * 1024
logger = logging.getLogger(__name__)


class _EnqueueReceipt(BaseModel):
    """原子提交的任务摘要和 Stream 标识，用于恢复不确定的入队结果。"""

    digest: str
    stream_id: str


class RedisConversationQueueStore:
    """Bounded Redis Stream plus durable retry/result state."""

    def __init__(
        self,
        redis: Redis,
        *,
        key_prefix: str,
        group_name: str,
        consumer_name: str,
        max_pending: int,
        result_ttl_seconds: int,
        image_task_store: RedisImageTaskStore | None = None,
    ) -> None:
        if not key_prefix or max_pending <= 0 or result_ttl_seconds <= 0:
            raise ValueError("invalid Redis queue store configuration")
        self._redis = redis
        self._group_name = group_name
        self._consumer_name = consumer_name
        self._max_pending = max_pending
        self._result_ttl_seconds = result_ttl_seconds
        self._image_task_store = image_task_store
        self._stream_key = f"{key_prefix}:jobs"
        self._state_key = f"{key_prefix}:state"
        self._result_prefix = f"{key_prefix}:result:"
        self._receipt_prefix = f"{key_prefix}:enqueued:"
        self._receipt_index_key = f"{key_prefix}:receipt-index"
        self._dead_letter_key = f"{key_prefix}:dead"
        self._reclaim_cursor = "-"
        self._recovery_complete = False

    @property
    def transient_errors(self) -> tuple[type[Exception], ...]:
        """返回队列编排层可以安全重试的 Redis 异常。"""

        return (RedisError,)

    async def initialize(self) -> None:
        """验证存储并重置单进程启动恢复游标。"""

        self._reclaim_cursor = "-"
        self._recovery_complete = False
        await self._redis.ping()
        try:
            await self._redis.xgroup_create(
                self._stream_key,
                self._group_name,
                id="0-0",
                mkstream=True,
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def enqueue(self, job: ConversationJob) -> str:
        """有界写入任务，后台绘图和 AI 结果按稳定任务标识原子去重。"""

        payload = job.model_dump_json(by_alias=True)
        limit = 384 * 1024 if job.ai_job_id is not None else _MAX_JOB_JSON_BYTES
        if len(payload.encode("utf-8")) > limit:
            raise ValueError("conversation queue job exceeds hard size limit")
        digest_payload = (
            job.model_dump_json(by_alias=True, exclude={"created_at"})
            if job.ai_job_id is not None else payload
        )
        digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
        deduplicate = (
            job.background_image or job.ai_job_id is not None
            or self._image_task_store is not None
        )
        receipt_key = self._receipt_prefix + hashlib.sha256(job.job_id.encode()).hexdigest()
        while True:
            async with self._redis.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(self._stream_key)
                    if deduplicate:
                        await pipeline.watch(receipt_key)
                        saved = await pipeline.get(receipt_key)
                        if saved is not None:
                            receipt = _EnqueueReceipt.model_validate_json(saved)
                            if receipt.digest != digest:
                                raise ValueError("deduplicated task parameters changed")
                            return receipt.stream_id
                    if await pipeline.xlen(self._stream_key) >= self._max_pending:
                        await pipeline.reset()
                        raise ConversationQueueFullError("conversation queue is full")
                    # 显式 Stream ID 让任务和去重回执在同一事务写入。
                    stream_id = "*"
                    if deduplicate:
                        info = (
                            await pipeline.xinfo_stream(self._stream_key)
                            if await pipeline.exists(self._stream_key) else None
                        )
                        now_ms = int(time.time() * 1000)
                        last_id = info["last-generated-id"] if info else None
                        last_ms, last_seq = map(int, last_id.split("-")) if last_id else (0, 0)
                        stream_id = (
                            f"{now_ms}-0" if now_ms > last_ms else f"{last_ms}-{last_seq + 1}"
                        )
                    pipeline.multi()
                    pipeline.xadd(self._stream_key, {"payload": payload}, id=stream_id)
                    if deduplicate:
                        pipeline.set(receipt_key, _EnqueueReceipt(
                            digest=digest, stream_id=stream_id,
                        ).model_dump_json(), ex=(
                            None if job.ai_job_id is not None or self._image_task_store is not None
                            else self._result_ttl_seconds
                        ))
                        if job.ai_job_id is not None or self._image_task_store is not None:
                            pipeline.hset(self._receipt_index_key, stream_id, receipt_key)
                    if self._image_task_store is not None:
                        self._image_task_store.enqueue(pipeline, job, stream_id)
                    result = await pipeline.execute()
                    return str(result[0])
                except WatchError:
                    continue

    async def read_new(self, *, count: int, block_ms: int) -> list[QueuedConversation]:
        """读取新任务，并在存储边界隔离损坏或旧版条目。"""

        records = await self._redis.xreadgroup(
            self._group_name,
            self._consumer_name,
            {self._stream_key: ">"},
            count=count,
            block=block_ms,
        )
        return await self._decode_records(records)

    async def reclaim(self, *, count: int, min_idle_ms: int) -> list[QueuedConversation]:
        """按全局 Stream 顺序有界恢复 pending，包括本实例重启留下的任务。"""

        if count <= 0:
            raise ValueError("reclaim count must be positive")
        if self._recovery_complete:
            return []
        pending = await self._redis.xpending_range(
            self._stream_key,
            self._group_name,
            min=self._reclaim_cursor,
            max="+",
            count=count,
            idle=min_idle_ms,
        )
        message_ids = [item["message_id"] for item in pending]
        if message_ids:
            records = await self._redis.xclaim(
                self._stream_key,
                self._group_name,
                self._consumer_name,
                min_idle_time=min_idle_ms,
                message_ids=message_ids,
            )
            decoded = await self._decode_entries(records)
            self._reclaim_cursor = f"({message_ids[-1]}"
            return decoded
        self._recovery_complete = True
        for consumer in await self._redis.xinfo_consumers(
            self._stream_key,
            self._group_name,
        ):
            consumer_name = consumer.get("name")
            if (
                isinstance(consumer_name, str)
                and consumer_name != self._consumer_name
                and consumer.get("pending") == 0
            ):
                await self._redis.xgroup_delconsumer(
                    self._stream_key,
                    self._group_name,
                    consumer_name,
                )
        return []

    @property
    def recovery_complete(self) -> bool:
        """指示单次启动扫描结束，编排器随后可开始领取新消息。"""

        return self._recovery_complete

    async def load_state(self, stream_id: str) -> QueueState:
        payload = await self._redis.hget(self._state_key, stream_id)
        return QueueState() if payload is None else QueueState.model_validate_json(payload)

    async def save_retry(self, stream_id: str, *, attempt: int, available_at: float) -> None:
        """更新重试进度，并保留生成结果及开画提示逐段投递状态。"""

        current = await self.load_state(stream_id)
        await self._save_state(
            stream_id,
            QueueState(
                attempt=attempt,
                available_at=available_at,
                delivery_index=current.delivery_index,
                result_ready=current.result_ready,
                image_start_delivery_index=current.image_start_delivery_index,
                image_start_delivered=current.image_start_delivered,
            ),
        )

    async def save_delivery_index(self, stream_id: str, delivery_index: int) -> None:
        """保存完成的结果投递位置，不改变开画提示的独立分段进度。"""

        current = await self.load_state(stream_id)
        await self._save_state(
            stream_id,
            QueueState(
                attempt=current.attempt,
                available_at=current.available_at,
                delivery_index=delivery_index,
                result_ready=current.result_ready,
                image_start_delivery_index=current.image_start_delivery_index,
                image_start_delivered=current.image_start_delivered,
            ),
        )

    async def save_image_start_delivered(self, stream_id: str) -> None:
        """保留开画分段进度并标记全部投递完成，兼容旧 v2 完成标记。"""

        current = await self.load_state(stream_id)
        await self._save_state(
            stream_id,
            QueueState(
                attempt=current.attempt,
                available_at=current.available_at,
                delivery_index=current.delivery_index,
                result_ready=current.result_ready,
                image_start_delivery_index=current.image_start_delivery_index,
                image_start_delivered=True,
            ),
        )

    async def save_image_start_delivery_index(self, stream_id: str, index: int) -> None:
        """仅在片段成功发送后持久化独立的开画提示进度。"""

        current = await self.load_state(stream_id)
        await self._save_state(
            stream_id,
            QueueState(
                attempt=current.attempt,
                available_at=current.available_at,
                delivery_index=current.delivery_index,
                result_ready=current.result_ready,
                image_start_delivery_index=index,
                image_start_delivered=current.image_start_delivered,
            ),
        )

    async def save_result(self, stream_id: str, result: ConversationResult) -> None:
        """持久化有界结果和开画进度，标记生成完成以避免投递重试重新生成。"""

        payload = result.model_dump_json(by_alias=True)
        # 原媒体与工具图片各自最多 10 MiB，Base64 组合仍受固定总上限约束。
        max_bytes = (
            _MAX_RESULT_WITH_TOOL_IMAGES_BYTES if result.tool_images else _MAX_RESULT_JSON_BYTES
        )
        if len(payload.encode("utf-8")) > max_bytes:
            raise ValueError("conversation result exceeds hard size limit")
        current = await self.load_state(stream_id)
        state = QueueState(
            attempt=current.attempt,
            available_at=current.available_at,
            delivery_index=current.delivery_index,
            result_ready=True,
            image_start_delivery_index=current.image_start_delivery_index,
            image_start_delivered=current.image_start_delivered,
        )
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.set(
                self._result_key(stream_id),
                payload,
                ex=self._result_ttl_seconds,
            )
            pipeline.hset(self._state_key, stream_id, state.model_dump_json())
            await pipeline.execute()

    async def load_result(self, stream_id: str) -> ConversationResult | None:
        """严格恢复 v2 结果；损坏结果隔离后发出不允许通知的终止信号。"""

        payload = await self._redis.get(self._result_key(stream_id))
        if payload is None:
            return None
        try:
            return ConversationResult.model_validate_json(payload)
        except ValidationError as exc:
            await self._discard_invalid(stream_id, exc)
            raise ConversationEntryDiscardedError("invalid persisted result discarded") from exc

    async def complete(self, stream_id: str) -> None:
        """投递和远端确认完成后，才允许 AI 结果的去重回执过期。"""
        receipt_key = await self._redis.hget(self._receipt_index_key, stream_id)
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xack(self._stream_key, self._group_name, stream_id)
            pipeline.xdel(self._stream_key, stream_id)
            pipeline.hdel(self._state_key, stream_id)
            pipeline.delete(self._result_key(stream_id))
            if receipt_key is not None:
                pipeline.expire(receipt_key, self._result_ttl_seconds)
                pipeline.hdel(self._receipt_index_key, stream_id)
            await pipeline.execute()

    async def dead_letter(
        self,
        entry: QueuedConversation,
        *,
        reason: str,
        attempt: int,
    ) -> None:
        fields = {
            "payload": entry.job.model_dump_json(by_alias=True),
            "sourceId": entry.stream_id,
            "reason": reason[:128],
            "attempt": str(attempt),
            "failedAt": str(time.time()),
        }
        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xadd(
                self._dead_letter_key,
                fields,
                maxlen=self._max_pending,
                approximate=True,
            )
            pipeline.xack(self._stream_key, self._group_name, entry.stream_id)
            pipeline.xdel(self._stream_key, entry.stream_id)
            pipeline.hdel(self._state_key, entry.stream_id)
            pipeline.delete(self._result_key(entry.stream_id))
            await pipeline.execute()

    async def source_length(self) -> int:
        return int(await self._redis.xlen(self._stream_key))

    async def dead_letter_length(self) -> int:
        return int(await self._redis.xlen(self._dead_letter_key))

    async def _save_state(self, stream_id: str, state: QueueState) -> None:
        await self._redis.hset(self._state_key, stream_id, state.model_dump_json())

    def _result_key(self, stream_id: str) -> str:
        return f"{self._result_prefix}{stream_id}"

    async def _decode_records(self, records: Sequence[object]) -> list[QueuedConversation]:
        """逐批解析记录，只返回验证成功的任务。"""

        decoded: list[QueuedConversation] = []
        for record in records:
            _, entries = record  # type: ignore[misc]
            decoded.extend(await self._decode_entries(entries))
        return decoded

    async def _decode_entries(self, entries: Sequence[object]) -> list[QueuedConversation]:
        """拒绝旧版或损坏任务，死信确认后继续读取其他记录。"""

        decoded: list[QueuedConversation] = []
        for raw_id, fields in entries:  # type: ignore[misc]
            payload = fields.get("payload")
            try:
                job = ConversationJob.model_validate_json(payload)
            except ValidationError as exc:
                await self._discard_invalid(str(raw_id), exc)
                continue
            decoded.append(
                QueuedConversation(
                    stream_id=str(raw_id),
                    job=job,
                )
            )
        return decoded

    async def _discard_invalid(self, stream_id: str, error: ValidationError) -> None:
        """原子写入无敏感内容的死信并确认、清理原始记录。"""

        async with self._redis.pipeline(transaction=True) as pipeline:
            pipeline.xadd(
                self._dead_letter_key,
                {"sourceId": stream_id, "reason": type(error).__name__},
                maxlen=self._max_pending,
                approximate=True,
            )
            pipeline.xack(self._stream_key, self._group_name, stream_id)
            pipeline.xdel(self._stream_key, stream_id)
            pipeline.hdel(self._state_key, stream_id)
            pipeline.delete(self._result_key(stream_id))
            await pipeline.execute()
        logger.warning(
            "Redis 队列条目解析失败并进入死信：stream_id=%s error=%s",
            stream_id,
            type(error).__name__,
        )


@dataclass(frozen=True, slots=True)
class ConversationQueueResources:
    """Redis 客户端、队列存储和处理队列的聚合资源。"""

    redis_client: Redis
    store: RedisConversationQueueStore
    queue: ConversationQueue
    image_queue: ConversationQueue


def build_conversation_queue(
    bootstrap: RedisBootstrapConfig,
    config: RuntimeConfig,
    processor: ConversationProcessor,
) -> ConversationQueueResources:
    """根据启动配置创建 Redis 队列及其所有权资源。"""

    redis_client = Redis(
        host=bootstrap.host,
        port=bootstrap.port,
        db=bootstrap.database,
        password=(
            bootstrap.password.get_secret_value()
            if bootstrap.password is not None
            else None
        ),
        ssl=bootstrap.ssl,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
        health_check_interval=30,
    )
    store = RedisConversationQueueStore(
        redis_client,
        key_prefix=bootstrap.key_prefix,
        group_name="dispatch-chat-workers",
        consumer_name=f"worker-{uuid.uuid4().hex}",
        max_pending=config.dispatch_chat_queue_max_pending,
        result_ttl_seconds=config.dispatch_chat_queue_result_ttl_seconds,
    )
    queue = ConversationQueue(
        store=store,
        processor=processor,
        policy=QueuePolicy(
            workers=config.dispatch_chat_queue_workers,
            media_workers=config.dispatch_chat_queue_media_workers,
            max_attempts=config.dispatch_chat_queue_max_attempts,
            retry_initial_seconds=config.dispatch_chat_queue_retry_initial_seconds,
            retry_max_seconds=config.dispatch_chat_queue_retry_max_seconds,
            job_ttl_seconds=config.dispatch_chat_queue_job_ttl_seconds,
        ),
    )
    image_tasks = RedisImageTaskStore(
        redis_client, key_prefix=f"{bootstrap.key_prefix}:images",
        result_ttl_seconds=config.dispatch_chat_queue_result_ttl_seconds,
        job_ttl_seconds=config.dispatch_chat_queue_job_ttl_seconds,
    )
    image_store = RedisConversationQueueStore(
        redis_client,
        key_prefix=f"{bootstrap.key_prefix}:images",
        group_name="dispatch-chat-image-workers",
        consumer_name=f"image-worker-{uuid.uuid4().hex}",
        max_pending=config.dispatch_chat_queue_max_pending,
        result_ttl_seconds=config.dispatch_chat_queue_result_ttl_seconds,
        image_task_store=image_tasks,
    )
    image_queue = ConversationQueue(
        store=image_store, processor=processor, policy=queue.policy,
        image_tasks=ImageTaskManager(image_tasks, image_store),
    )
    return ConversationQueueResources(
        redis_client=redis_client,
        store=store,
        queue=queue,
        image_queue=image_queue,
    )
