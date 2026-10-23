"""用 Redis 原子占位隔离聊天工具的重复副作用。"""

from __future__ import annotations

import hashlib

from redis.asyncio import Redis

from agent_bot.services.tool_operations import (
    DeliveryEffect,
    EffectToolName,
    ImageEffect,
    ImageTaskEffect,
    MentionEffect,
    ReplyEffect,
    SpeechEffect,
    ToolEffect,
    ToolOperation,
    ToolOperationConflict,
    VideoEffect,
)


class RedisToolOperationStore:
    """记录保留时间至少覆盖任务寿命，不确定结果不会自动获得新执行权。"""

    def __init__(self, redis: Redis, *, key_prefix: str, ttl_seconds: int) -> None:
        """复用队列的 Redis 连接池，并设置有界结果保留时间。"""

        if not 1 <= ttl_seconds <= 86_400 or not key_prefix:
            raise ValueError("tool operation retention is invalid")
        self._redis = redis
        self._prefix = f"{key_prefix}:tool-operation"
        self._ttl = ttl_seconds

    async def begin(
        self, job_id: str, tool: EffectToolName, signature: str,
    ) -> ToolEffect | None:
        """原子占用唯一执行权，或返回同参数的已完成结果。"""

        key = self._key(job_id, tool)
        operation = ToolOperation(signature=signature)
        if await self._redis.set(key, operation.model_dump_json(), ex=self._ttl, nx=True):
            return None
        existing = await self._redis.get(key)
        if existing is None:
            raise ToolOperationConflict("工具执行状态已经失效，请发起新请求")
        record = ToolOperation.model_validate_json(existing)
        if record.signature != signature or record.status != "COMPLETED":
            raise ToolOperationConflict("工具已执行或结果尚未确认，本次请求不重复执行")
        self._validate_result(tool, record.result)
        return record.result

    async def complete(
        self, job_id: str, tool: EffectToolName, signature: str, result: ToolEffect,
    ) -> None:
        """在副作用完成后写入结果，状态丢失时保持不确定而不重新执行。"""

        key = self._key(job_id, tool)
        self._validate_result(tool, result)
        record = ToolOperation(signature=signature, status="COMPLETED", result=result)
        if tool == "outgoing_message" and await self._redis.set(
            key, record.model_dump_json(), ex=self._ttl, nx=True,
        ):
            # 平台确认后的首次回执不需要另一个执行占位，主库日志已负责防止重复发送。
            return
        async with self._redis.pipeline(transaction=True) as pipeline:
            await pipeline.watch(key)
            raw = await pipeline.get(key)
            if raw is None or ToolOperation.model_validate_json(raw).signature != signature:
                raise ToolOperationConflict("工具执行状态不可用")
            pipeline.multi()
            pipeline.set(key, record.model_dump_json(), ex=self._ttl, xx=True)
            saved = await pipeline.execute()
            if not saved[0]:
                raise ToolOperationConflict("工具执行状态不可用")

    async def completed(self, job_id: str, tool: EffectToolName) -> ToolEffect | None:
        """恢复本任务已经生成的媒体；尚未执行或状态不明时不伪造结果。"""

        raw = await self._redis.get(self._key(job_id, tool))
        if raw is None:
            return None
        result = ToolOperation.model_validate_json(raw).result
        if result is not None:
            self._validate_result(tool, result)
        return result

    def _validate_result(self, tool: EffectToolName, result: ToolEffect | None) -> None:
        """持久结果必须属于授权工具，防止媒体类型在恢复时串用。"""

        if (
            (tool in {"send_reply", "image_start_reply", "image_completion_reply"}
             and not isinstance(result, ReplyEffect))
            or (tool == "mention_member" and not isinstance(result, MentionEffect))
            or (tool == "local_workflow_generate_image"
                and not isinstance(result, (ImageEffect, ImageTaskEffect)))
            or (tool == "local_workflow_generate_video"
                and not isinstance(result, (VideoEffect, ImageTaskEffect)))
            or (tool == "image_generate" and not isinstance(result, ImageTaskEffect))
            or (tool == "outgoing_message" and not isinstance(result, DeliveryEffect))
            or (tool == "tts_generate" and not isinstance(result, SpeechEffect))
        ):
            raise ValueError("tool operation result type is invalid")

    def _key(self, job_id: str, tool: EffectToolName) -> str:
        """限制任务和已登记工具标识，直接生图回执也按任务独立持久化。"""

        if (
            not job_id
            or len(job_id) > 128
            or tool
            not in {
                "send_reply",
                "mention_member",
                "local_workflow_generate_image",
                "local_workflow_generate_video",
                "tts_generate",
                "image_start_reply",
                "image_completion_reply",
                "image_generate",
                "outgoing_message",
            }
        ):
            raise ValueError("tool operation identity is invalid")
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        return f"{self._prefix}:{digest}:{tool}"
