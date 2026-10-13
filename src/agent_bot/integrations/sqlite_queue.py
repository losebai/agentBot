"""无需 Redis 的 SQLite 持久会话队列及其装配资源。"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import ValidationError

from agent_bot.config import RuntimeConfig
from agent_bot.integrations.sqlite_queue_database import (
    SqliteQueueBusyError,
    initialize_queue_database,
    run_sqlite,
)
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

if TYPE_CHECKING:
    from agent_bot.integrations.sqlite_image_tasks import SqliteImageTaskStore


class SqliteConversationQueueStore:
    """单进程有界队列，事务保存去重回执、结果与重启恢复检查点。"""

    def __init__(
        self, path: str, *, key_prefix: str, max_pending: int,
        result_ttl_seconds: int, image_task_store: SqliteImageTaskStore | None = None,
    ) -> None:
        """使用独立队列文件和命名空间，构造阶段不执行阻塞磁盘操作。"""
        if not path or not key_prefix or max_pending <= 0 or result_ttl_seconds <= 0:
            raise ValueError("invalid SQLite queue store configuration")
        self.path = path
        self._namespace = key_prefix
        self._max_pending = max_pending
        self._result_ttl_seconds = result_ttl_seconds
        self._image_task_store = image_task_store
        self._reclaim_cursor = 0
        self._recovery_complete = False

    @property
    def transient_errors(self) -> tuple[type[Exception], ...]:
        """只有数据库锁竞争允许重试，配置或结构损坏应明确失败。"""
        return (SqliteQueueBusyError,)

    async def initialize(self) -> None:
        """初始化选中的本地队列，并开启一次按原始顺序的恢复扫描。"""
        await asyncio.to_thread(initialize_queue_database, self.path)
        self._reclaim_cursor = 0
        self._recovery_complete = False

    @property
    def recovery_complete(self) -> bool:
        """报告本轮 pending 扫描是否结束。"""
        return self._recovery_complete

    async def enqueue(self, job: ConversationJob) -> str:
        """在一个事务中检查容量、去重并写入图片元数据。"""
        payload = job.model_dump_json(by_alias=True)
        limit = 384 * 1024 if job.ai_job_id is not None else 128 * 1024
        if len(payload.encode("utf-8")) > limit:
            raise ValueError("conversation queue job exceeds hard size limit")
        digest_payload = (
            job.model_dump_json(by_alias=True, exclude={"created_at"})
            if job.ai_job_id is not None else payload
        )
        digest = hashlib.sha256(digest_payload.encode("utf-8")).hexdigest()
        deduplicate = (
            job.background_image or job.ai_job_id is not None or self._image_task_store is not None
        )

        def insert(connection: sqlite3.Connection) -> str:
            """序列化容量与回执检查，防止并发提交突破容量或重复副作用。"""
            connection.execute(
                "DELETE FROM chat_queue_receipts WHERE namespace = ? AND expires_at <= ?",
                (self._namespace, time.time()),
            )
            if deduplicate:
                saved = connection.execute(
                    "SELECT digest, stream_id FROM chat_queue_receipts "
                    "WHERE namespace = ? AND job_id = ?", (self._namespace, job.job_id),
                ).fetchone()
                if saved is not None:
                    if saved["digest"] != digest:
                        raise ValueError("deduplicated task parameters changed")
                    return str(saved["stream_id"])
            size = connection.execute(
                "SELECT COUNT(*) FROM chat_queue_jobs WHERE namespace = ?", (self._namespace,),
            ).fetchone()[0]
            if size >= self._max_pending:
                raise ConversationQueueFullError("conversation queue is full")
            cursor = connection.execute(
                "INSERT INTO chat_queue_jobs(namespace, payload, state) VALUES (?, ?, ?)",
                (self._namespace, payload, QueueState().model_dump_json()),
            )
            stream_id = str(cursor.lastrowid)
            if deduplicate:
                connection.execute(
                    "INSERT INTO chat_queue_receipts(namespace, job_id, digest, stream_id) "
                    "VALUES (?, ?, ?, ?)", (self._namespace, job.job_id, digest, stream_id),
                )
            if self._image_task_store is not None:
                self._image_task_store.enqueue(connection, job, stream_id)
            return stream_id

        return await run_sqlite(self.path, insert)

    async def read_new(self, *, count: int, block_ms: int) -> list[QueuedConversation]:
        """非阻塞轮询新任务，SQLite 查询本身在线程中执行。"""
        if count <= 0 or block_ms < 0:
            raise ValueError("invalid queue read limits")
        deadline = time.monotonic() + block_ms / 1000
        while True:
            entries, last_id = await self._claim(count=count, reclaim=False, min_idle_ms=0)
            if last_id is not None or time.monotonic() >= deadline:
                return entries
            await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))

    async def reclaim(self, *, count: int, min_idle_ms: int) -> list[QueuedConversation]:
        """有界扫描旧 pending，每次重启仅恢复一遍，保持原始任务顺序。"""
        if count <= 0 or min_idle_ms < 0:
            raise ValueError("invalid queue reclaim limits")
        if self._recovery_complete:
            return []
        entries, last_id = await self._claim(count=count, reclaim=True, min_idle_ms=min_idle_ms)
        if last_id is None:
            self._recovery_complete = True
        else:
            self._reclaim_cursor = last_id
        return entries

    async def _claim(
        self, *, count: int, reclaim: bool, min_idle_ms: int,
    ) -> tuple[list[QueuedConversation], int | None]:
        """同一事务选择并领取任务，损坏载荷隔离后仍推进恢复游标。"""
        def claim(connection: sqlite3.Connection) -> tuple[list[QueuedConversation], int | None]:
            """按顺序领取并验证载荷，避免损坏内容进入处理器。"""
            now = time.time()
            if reclaim:
                rows = connection.execute(
                    "SELECT stream_id, payload FROM chat_queue_jobs "
                    "WHERE namespace = ? AND stream_id > ? AND claimed_at <= ? "
                    "ORDER BY stream_id LIMIT ?",
                    (self._namespace, self._reclaim_cursor, now - min_idle_ms / 1000, count),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT stream_id, payload FROM chat_queue_jobs "
                    "WHERE namespace = ? AND claimed_at IS NULL ORDER BY stream_id LIMIT ?",
                    (self._namespace, count),
                ).fetchall()
            entries = []
            for row in rows:
                stream_id = str(row["stream_id"])
                try:
                    job = ConversationJob.model_validate_json(row["payload"])
                except ValidationError:
                    self._dead_letter(connection, stream_id, None, "ValidationError", 0)
                    continue
                connection.execute(
                    "UPDATE chat_queue_jobs SET claimed_at = ? "
                    "WHERE namespace = ? AND stream_id = ?", (now, self._namespace, stream_id),
                )
                entries.append(QueuedConversation(stream_id=stream_id, job=job))
            return entries, rows[-1]["stream_id"] if rows else None

        return await run_sqlite(self.path, claim)

    def _load_state(self, connection: sqlite3.Connection, stream_id: str) -> QueueState:
        """在调用者事务内读取检查点，不存在时使用协议默认状态。"""
        row = connection.execute(
            "SELECT state FROM chat_queue_jobs WHERE namespace = ? AND stream_id = ?",
            (self._namespace, stream_id),
        ).fetchone()
        return QueueState() if row is None else QueueState.model_validate_json(row["state"])

    async def load_state(self, stream_id: str) -> QueueState:
        """读取持久检查点。"""
        return await run_sqlite(
            self.path, lambda connection: self._load_state(connection, stream_id),
        )

    async def _update_state(self, stream_id: str, **changes: object) -> None:
        """合并更新检查点，保留其他步骤已成功写入的进度。"""
        def update(connection: sqlite3.Connection) -> None:
            """事务内读改写，已完成或取消的条目不会被重新创建。"""
            state = self._load_state(connection, stream_id).model_copy(update=changes)
            connection.execute(
                "UPDATE chat_queue_jobs SET state = ? WHERE namespace = ? AND stream_id = ?",
                (state.model_dump_json(), self._namespace, stream_id),
            )

        await run_sqlite(self.path, update)

    async def save_retry(self, stream_id: str, *, attempt: int, available_at: float) -> None:
        """保存退避时间与次数，不影响已生成结果及两种投递位置。"""
        await self._update_state(stream_id, attempt=attempt, available_at=available_at)

    async def save_delivery_index(self, stream_id: str, delivery_index: int) -> None:
        """只在平台确认结果片段成功后记录位置。"""
        await self._update_state(stream_id, delivery_index=delivery_index)

    async def save_image_start_delivery_index(self, stream_id: str, index: int) -> None:
        """独立保存开画提示的分段位置。"""
        await self._update_state(stream_id, image_start_delivery_index=index)

    async def save_image_start_delivered(self, stream_id: str) -> None:
        """标记开画提示全部发送完成。"""
        await self._update_state(stream_id, image_start_delivered=True)

    async def save_result(self, stream_id: str, result: ConversationResult) -> None:
        """原子保存生成结果和完成标记，避免投递重试重复调用模型。"""
        payload = result.model_dump_json(by_alias=True)
        limit = (32 if result.tool_images else 16) * 1024 * 1024
        if len(payload.encode("utf-8")) > limit:
            raise ValueError("conversation result exceeds hard size limit")

        def save(connection: sqlite3.Connection) -> None:
            """结果与检查点必须一同提交或回滚。"""
            state = self._load_state(connection, stream_id).model_copy(
                update={"result_ready": True},
            )
            connection.execute(
                "UPDATE chat_queue_jobs SET result = ?, result_expires_at = ?, state = ? "
                "WHERE namespace = ? AND stream_id = ?",
                (payload, time.time() + self._result_ttl_seconds, state.model_dump_json(),
                 self._namespace, stream_id),
            )

        await run_sqlite(self.path, save)

    async def load_result(self, stream_id: str) -> ConversationResult | None:
        """过期结果不重生成，损坏结果提交死信清理后才通知编排器停止。"""
        def read(connection: sqlite3.Connection) -> ConversationResult | bool | None:
            """返回损坏标记而非事务内抛错，确保死信操作不会回滚。"""
            row = connection.execute(
                "SELECT result, result_expires_at FROM chat_queue_jobs "
                "WHERE namespace = ? AND stream_id = ?", (self._namespace, stream_id),
            ).fetchone()
            if row is None or row["result"] is None:
                return None
            if row["result_expires_at"] <= time.time():
                connection.execute(
                    "UPDATE chat_queue_jobs SET result = NULL, result_expires_at = NULL "
                    "WHERE namespace = ? AND stream_id = ?", (self._namespace, stream_id),
                )
                return None
            try:
                return ConversationResult.model_validate_json(row["result"])
            except ValidationError:
                self._dead_letter(connection, stream_id, None, "ValidationError", 0)
                return False

        result = await run_sqlite(self.path, read)
        if result is False:
            raise ConversationEntryDiscardedError("invalid persisted result discarded")
        return result

    def _complete(self, connection: sqlite3.Connection, stream_id: str) -> None:
        """释放任务容量并开始回执保留期，禁止完成后立即重复生成。"""
        connection.execute(
            "DELETE FROM chat_queue_jobs WHERE namespace = ? AND stream_id = ?",
            (self._namespace, stream_id),
        )
        connection.execute(
            "UPDATE chat_queue_receipts SET expires_at = ? "
            "WHERE namespace = ? AND stream_id = ? AND expires_at IS NULL",
            (time.time() + self._result_ttl_seconds, self._namespace, stream_id),
        )

    async def complete(self, stream_id: str) -> None:
        """成功、已取消或已结束的任务都使用幂等确认清理。"""
        await run_sqlite(self.path, lambda connection: self._complete(connection, stream_id))

    def _dead_letter(
        self, connection: sqlite3.Connection, stream_id: str, payload: str | None,
        reason: str, attempt: int,
    ) -> None:
        """写入有界死信并清理源任务，保证失败记录与容量释放一致。"""
        connection.execute(
            "INSERT INTO chat_queue_deadletters"
            "(namespace, source_id, payload, reason, attempt, failed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (self._namespace, stream_id, payload, reason[:128], attempt, time.time()),
        )
        connection.execute(
            "DELETE FROM chat_queue_deadletters WHERE namespace = ? AND id NOT IN "
            "(SELECT id FROM chat_queue_deadletters WHERE namespace = ? ORDER BY id DESC LIMIT ?)",
            (self._namespace, self._namespace, self._max_pending),
        )
        self._complete(connection, stream_id)

    async def dead_letter(
        self, entry: QueuedConversation, *, reason: str, attempt: int,
    ) -> None:
        """业务最终失败时保留合法载荷与稳定错误类型。"""
        await run_sqlite(self.path, lambda connection: self._dead_letter(
            connection, entry.stream_id, entry.job.model_dump_json(by_alias=True), reason, attempt,
        ))

    async def source_length(self) -> int:
        """统计当前命名空间仍占用容量的任务数。"""
        return await run_sqlite(self.path, lambda connection: connection.execute(
            "SELECT COUNT(*) FROM chat_queue_jobs WHERE namespace = ?", (self._namespace,),
        ).fetchone()[0])

    async def dead_letter_length(self) -> int:
        """统计当前命名空间已保留的死信数。"""
        return await run_sqlite(self.path, lambda connection: connection.execute(
            "SELECT COUNT(*) FROM chat_queue_deadletters WHERE namespace = ?", (self._namespace,),
        ).fetchone()[0])


@dataclass(frozen=True, slots=True)
class SqliteConversationQueueResources:
    """拥有本地文本、图片队列的生命周期资源。"""

    store: SqliteConversationQueueStore
    queue: ConversationQueue
    image_queue: ConversationQueue

    async def ping(self) -> bool:
        """读取已初始化的队列表，检测文件及结构可用性。"""
        await self.store.source_length()
        return True

    async def aclose(self) -> None:
        """短事务每次自行关闭连接，停止队列即可完成资源释放。"""
        await self.queue.stop()
        await self.image_queue.stop()


def build_sqlite_conversation_queue(
    path: str, key_prefix: str, config: RuntimeConfig, processor: ConversationProcessor,
) -> SqliteConversationQueueResources:
    """装配本地持久文本与图片队列，共享已验证的业务编排逻辑。"""
    from agent_bot.integrations.sqlite_image_tasks import SqliteImageTaskStore

    store = SqliteConversationQueueStore(
        path, key_prefix=key_prefix, max_pending=config.chat_queue_max_pending,
        result_ttl_seconds=config.chat_queue_result_ttl_seconds,
    )
    policy = QueuePolicy(
        workers=config.chat_queue_workers, media_workers=config.chat_queue_media_workers,
        max_attempts=config.chat_queue_max_attempts,
        retry_initial_seconds=config.chat_queue_retry_initial_seconds,
        retry_max_seconds=config.chat_queue_retry_max_seconds,
        job_ttl_seconds=config.chat_queue_job_ttl_seconds,
    )
    queue = ConversationQueue(store=store, processor=processor, policy=policy)
    image_tasks = SqliteImageTaskStore(
        path, key_prefix=f"{key_prefix}:images",
        result_ttl_seconds=config.chat_queue_result_ttl_seconds,
        job_ttl_seconds=config.chat_queue_job_ttl_seconds,
    )
    image_store = SqliteConversationQueueStore(
        path, key_prefix=f"{key_prefix}:images", max_pending=config.chat_queue_max_pending,
        result_ttl_seconds=config.chat_queue_result_ttl_seconds, image_task_store=image_tasks,
    )
    image_queue = ConversationQueue(
        store=image_store, processor=processor, policy=policy,
        image_tasks=ImageTaskManager(image_tasks, image_store),
    )
    return SqliteConversationQueueResources(store=store, queue=queue, image_queue=image_queue)
