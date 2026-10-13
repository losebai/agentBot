"""工具登记的请求级提醒目标与意图。"""

import asyncio
import hashlib

from agent_bot.services.tool_operations import (
    ReplyEffect,
    ToolOperationConflict,
    ToolOperationStore,
)


class ChatReplyCollector:
    """保存提醒元数据；实际回复文字由 Agent 最终正文提供。"""

    def __init__(self) -> None:
        """限制同一请求只登记一个提醒目标和意图。"""
        self.reply: ReplyEffect | None = None
        self.already_delivered = False
        self._lock = asyncio.Lock()

    async def select(
        self, reply: ReplyEffect, *, job_id: str | None, store: ToolOperationStore | None,
    ) -> ReplyEffect:
        """允许重试复用提醒意图，防止同一请求登记互相冲突的提醒。"""
        async with self._lock:
            if self.reply is not None:
                if self.reply != reply:
                    raise ToolOperationConflict("本次请求已经登记提醒，不能改为另一条提醒")
                return self.reply
            if store is not None and job_id is not None:
                signature = hashlib.sha256(reply.model_dump_json().encode("utf-8")).hexdigest()
                # 沿用已部署队列的持久化键；该名称不再作为 MCP 工具暴露。
                saved = await store.begin(job_id, "send_reply", signature)
                if saved is not None:
                    if not isinstance(saved, ReplyEffect):
                        raise ValueError("saved reply has invalid type")
                    reply = saved
                else:
                    await store.complete(job_id, "send_reply", signature, reply)
            self.reply = reply
            return reply
