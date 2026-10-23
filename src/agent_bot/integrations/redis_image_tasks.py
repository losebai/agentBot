"""图片任务元数据及有界归属索引的 Redis 持久化。"""
from __future__ import annotations

import hashlib
import math
import time
from typing import TYPE_CHECKING

from redis.asyncio import Redis
from redis.exceptions import WatchError

from agent_bot.services.image_tasks import ImageTaskError, ImageTaskOwner, ImageTaskRecord

if TYPE_CHECKING:
    from redis.asyncio.client import Pipeline

    from agent_bot.services.conversation_queue import ConversationJob


class RedisImageTaskStore:
    """原子入队集成以及不在任务存活时过期的取消记录。"""

    def __init__(self, redis: Redis, *, key_prefix: str, result_ttl_seconds: int,
                 job_ttl_seconds: float) -> None:
        """与图片 Stream 共用 Redis 连接与保留策略。"""
        self.redis = redis
        self.prefix = f"{key_prefix}:task:"
        self.result_ttl = result_ttl_seconds
        self.job_ttl = job_ttl_seconds

    def _key(self, task_id: str) -> str:
        """散列不可信标识，避免 Redis 键命名空间冲突。"""
        return self.prefix + hashlib.sha256(task_id.encode()).hexdigest()

    def _index(self, owner: ImageTaskOwner) -> str:
        """将三个归属字段共同编码，杜绝分隔符碰撞。"""
        return self.prefix + "owner:" + hashlib.sha256(owner.model_dump_json().encode()).hexdigest()

    def enqueue(self, pipeline: Pipeline, job: ConversationJob, stream_id: str) -> None:
        """在 Stream 的同一个事务中加入任务元数据和有界索引。"""
        owner = ImageTaskOwner(bot_id=job.bot_id, session_id=job.session_id, user_id=job.user_id)
        record = ImageTaskRecord(task_id=job.job_id, created_at=job.created_at,
            media_kind=job.workflow_request.output_kind if job.workflow_request else "image",
                                 owner=owner, stream_id=stream_id)
        pipeline.set(self._key(job.job_id), record.model_dump_json())
        index = self._index(owner)
        pipeline.zadd(index, {job.job_id: job.created_at})
        pipeline.zremrangebyrank(index, 0, -21)
        pipeline.expire(index, math.ceil(self.job_ttl + self.result_ttl))

    async def get(self, task_id: str) -> ImageTaskRecord | None:
        """读取并验证任务内部记录。"""
        value = await self.redis.get(self._key(task_id))
        return ImageTaskRecord.model_validate_json(value) if value is not None else None

    async def adopt(self, record: ImageTaskRecord) -> None:
        """事务补录升级前任务；并发取消或另一个恢复者不能被覆盖。"""
        key = self._key(record.task_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(key)
                    if await pipeline.exists(key):
                        return
                    index = self._index(record.owner)
                    pipeline.multi()
                    pipeline.set(key, record.model_dump_json())
                    pipeline.zadd(index, {record.task_id: record.created_at})
                    pipeline.zremrangebyrank(index, 0, -21)
                    pipeline.expire(index, math.ceil(self.job_ttl + self.result_ttl))
                    await pipeline.execute()
                    return
                except WatchError:
                    continue

    async def save(self, record: ImageTaskRecord) -> None:
        """活动任务无 TTL；取消终态覆盖可能仍在内存中的整个任务预算。"""
        key = self._key(record.task_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(key)
                    previous = await pipeline.get(key)
                    if previous is not None:
                        saved = ImageTaskRecord.model_validate_json(previous)
                        if saved.cancellation_requested:
                            record.cancellation_requested = True
                            record.status = "cancelled"
                            record.settled = record.settled or saved.settled
                            if record.submitting and record.prompt_id is None:
                                record.upstream_cancellation = "pending"
                    ttl = None
                    if record.settled:
                        ttl = self.result_ttl
                        if record.cancellation_requested:
                            ttl += max(0, math.ceil(
                                record.created_at + self.job_ttl - time.time(),
                            ))
                    pipeline.multi()
                    pipeline.set(key, record.model_dump_json(), ex=ttl)
                    await pipeline.execute()
                    return
                except WatchError:
                    continue

    async def request_cancel(self, owner: ImageTaskOwner, task_id: str) -> ImageTaskRecord:
        """先于发送锁持久化意图，任何状态写入都不能清除取消标记。"""
        key = self._key(task_id)
        while True:
            async with self.redis.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(key)
                    value = await pipeline.get(key)
                    record = ImageTaskRecord.model_validate_json(value) if value else None
                    if record is None or record.owner != owner:
                        raise ImageTaskError(
                            "image_task_not_found", "绘图任务不存在或不属于当前用户",
                        )
                    if record.status == "completed" or (
                        record.status == "failed" and record.prompt_id is None
                        and not record.submitting
                    ):
                        return record
                    record.cancellation_requested = True
                    record.status = "cancelled"
                    if record.submitting and record.prompt_id is None:
                        record.upstream_cancellation = "pending"
                    elif record.started and record.prompt_id is None:
                        record.upstream_cancellation = "unsupported"
                    pipeline.multi()
                    pipeline.set(key, record.model_dump_json(), keepttl=True)
                    await pipeline.execute()
                    return record
                except WatchError:
                    continue

    async def recent(self, owner: ImageTaskOwner, limit: int) -> list[ImageTaskRecord]:
        """批量读取最多二十项，清理已过期索引并再次核对归属。"""
        index = self._index(owner)
        identifiers = await self.redis.zrevrange(index, 0, limit - 1)
        if not identifiers:
            return []
        values = await self.redis.mget([self._key(identifier) for identifier in identifiers])
        records = []
        for identifier, value in zip(identifiers, values, strict=True):
            if value is None:
                await self.redis.zrem(index, identifier)
                continue
            record = ImageTaskRecord.model_validate_json(value)
            if record.owner == owner:
                records.append(record)
        return records
