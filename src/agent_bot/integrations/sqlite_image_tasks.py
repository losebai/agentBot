"""与本地队列共用事务的 SQLite 图片任务和不可撤销取消记录。"""

from __future__ import annotations

import math
import sqlite3
import time
from typing import TYPE_CHECKING

from agent_bot.integrations.sqlite_queue_database import run_sqlite
from agent_bot.services.image_tasks import ImageTaskError, ImageTaskOwner, ImageTaskRecord

if TYPE_CHECKING:
    from agent_bot.services.conversation_queue import ConversationJob


class SqliteImageTaskStore:
    """活动任务不设置过期时间，状态变更在数据库写事务中合并取消意图。"""

    def __init__(
        self, path: str, *, key_prefix: str, result_ttl_seconds: int, job_ttl_seconds: float,
    ) -> None:
        """保存队列数据库位置和命名空间，建表统一交给队列初始化。"""
        self.path = path
        self.prefix = key_prefix
        self.result_ttl = result_ttl_seconds
        self.job_ttl = job_ttl_seconds

    def enqueue(
        self, connection: sqlite3.Connection, job: ConversationJob, stream_id: str,
    ) -> None:
        """借用队列的未提交事务写入元数据，入队失败时一同回滚。"""
        self._write(connection, ImageTaskRecord(
            task_id=job.job_id, created_at=job.created_at, stream_id=stream_id,
            owner=ImageTaskOwner(bot_id=job.bot_id, session_id=job.session_id, user_id=job.user_id),
        ))

    def _purge(self, connection: sqlite3.Connection) -> None:
        """清理本命名空间已结束且超出保留期的记录，活动记录始终保留。"""
        connection.execute(
            "DELETE FROM chat_image_tasks WHERE namespace=? AND expires_at<=?",
            (self.prefix, time.time()),
        )

    def _read(
        self, connection: sqlite3.Connection, task_id: str,
    ) -> ImageTaskRecord | None:
        """只读取当前命名空间中仍有效的任务。"""
        row = connection.execute(
            "SELECT payload FROM chat_image_tasks WHERE namespace=? AND task_id=? "
            "AND (expires_at IS NULL OR expires_at>?)",
            (self.prefix, task_id, time.time()),
        ).fetchone()
        return ImageTaskRecord.model_validate_json(row[0]) if row else None

    def _write(
        self, connection: sqlite3.Connection, record: ImageTaskRecord,
    ) -> None:
        """只在任务结束后设置 TTL，取消状态覆盖旧内存任务的完整执行预算。"""
        now = time.time()
        expires_at = None
        if record.settled:
            expires_at = now + self.result_ttl
            if record.cancellation_requested:
                expires_at += max(0, math.ceil(record.created_at + self.job_ttl - now))
        connection.execute(
            "INSERT INTO chat_image_tasks "
            "(namespace,task_id,owner,created_at,payload,expires_at) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(namespace,task_id) DO UPDATE SET owner=excluded.owner, "
            "created_at=excluded.created_at,payload=excluded.payload,expires_at=excluded.expires_at",
            (self.prefix, record.task_id, record.owner.model_dump_json(), record.created_at,
             record.model_dump_json(), expires_at),
        )

    async def get(self, task_id: str) -> ImageTaskRecord | None:
        """在线程中读取任务，不阻塞机器人事件循环。"""
        def read(connection: sqlite3.Connection) -> ImageTaskRecord | None:
            """读取前回收过期结果。"""
            self._purge(connection)
            return self._read(connection, task_id)

        return await run_sqlite(self.path, read)

    async def adopt(self, record: ImageTaskRecord) -> None:
        """仅补录缺失任务，多个恢复者不能覆盖现有状态及取消标志。"""
        def adopt(connection: sqlite3.Connection) -> None:
            """在同一写事务中检查并补录。"""
            self._purge(connection)
            if self._read(connection, record.task_id) is None:
                self._write(connection, record)

        await run_sqlite(self.path, adopt)

    async def save(self, record: ImageTaskRecord) -> None:
        """原子合并已持久化的取消标记，并反馈给仍执行中的调用者。"""
        def save(connection: sqlite3.Connection) -> None:
            """旧快照永远不能将已取消任务恢复为运行态。"""
            self._purge(connection)
            saved = self._read(connection, record.task_id)
            if saved is not None and saved.cancellation_requested:
                record.cancellation_requested = True
                record.status = "cancelled"
                record.settled = record.settled or saved.settled
                if record.submitting and record.prompt_id is None:
                    record.upstream_cancellation = "pending"
            self._write(connection, record)

        await run_sqlite(self.path, save)

    async def request_cancel(self, owner: ImageTaskOwner, task_id: str) -> ImageTaskRecord:
        """在同一事务中验证完整归属并固化取消，不向外泄露其他用户任务。"""
        def cancel(connection: sqlite3.Connection) -> ImageTaskRecord:
            """保留原 TTL，提交中的上游任务先记录待取消状态。"""
            self._purge(connection)
            record = self._read(connection, task_id)
            if record is None or record.owner != owner:
                raise ImageTaskError("image_task_not_found", "绘图任务不存在或不属于当前用户")
            if record.status == "completed" or (
                record.status == "failed" and record.prompt_id is None and not record.submitting
            ):
                return record
            record.cancellation_requested = True
            record.status = "cancelled"
            if record.submitting and record.prompt_id is None:
                record.upstream_cancellation = "pending"
            elif record.started and record.prompt_id is None:
                record.upstream_cancellation = "unsupported"
            connection.execute(
                "UPDATE chat_image_tasks SET payload=? WHERE namespace=? AND task_id=?",
                (record.model_dump_json(), self.prefix, task_id),
            )
            return record

        return await run_sqlite(self.path, cancel)

    async def recent(self, owner: ImageTaskOwner, limit: int) -> list[ImageTaskRecord]:
        """查询完整归属下的最近最多二十条有效任务。"""
        def recent(connection: sqlite3.Connection) -> list[ImageTaskRecord]:
            """借助归属索引限制查询规模，反序列化后再次核对权限。"""
            self._purge(connection)
            rows = connection.execute(
                "SELECT payload FROM chat_image_tasks WHERE namespace=? AND owner=? "
                "ORDER BY created_at DESC,task_id DESC LIMIT ?",
                (self.prefix, owner.model_dump_json(), max(0, min(limit, 20))),
            ).fetchall()
            records = [ImageTaskRecord.model_validate_json(row[0]) for row in rows]
            return [record for record in records if record.owner == owner]

        return await run_sqlite(self.path, recent)
