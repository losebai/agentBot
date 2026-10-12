"""主动拉取 AI 任务结果并交给持久消息队列。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager

from agent_bot.config import PluginConfig
from agent_bot.integrations.dispatch_ai import DispatchAiClient
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationQueue,
    PersistedGeneratedReply,
)
from agent_bot.state import AccessPolicy

logger = logging.getLogger(__name__)


class AiJobPoller:
    """轮询使用当前运行时快照，热刷新无需替换或重启轮询任务。"""

    def __init__(
        self,
        *,
        config_provider: Callable[[], PluginConfig],
        client_provider: Callable[[], DispatchAiClient],
        queue_provider: Callable[[], ConversationQueue],
        bot_ids_provider: Callable[[], Sequence[str]],
        runtime_lease: Callable[[], AbstractAsyncContextManager[None]],
    ) -> None:
        """按提供器读取热更新配置、已连接机器人与持久队列。"""
        self._config = config_provider
        self._client = client_provider
        self._queue = queue_provider
        self._bots = bot_ids_provider
        self._lease = runtime_lease
        self._cursors: dict[str, str | None] = {}

    async def run(self) -> None:
        """周期完成后等待，不补发错过的轮询周期。"""
        while True:
            await asyncio.sleep(self._config().dispatch_chat_job_poll_seconds)
            try:
                await self.run_once()
            except Exception as error:
                logger.warning("AI 任务结果拉取失败：error=%s", type(error).__name__)

    async def run_once(self) -> None:
        """只将当前允许目标的正文持久入队，由投递完成流程确认消费。"""
        async with self._lease():
            config = self._config()
            if not config.dispatch_chat_job_enabled:
                return
            policy = AccessPolicy(
                allowed_users=config.dispatch_chat_allowed_users,
                allowed_groups=config.dispatch_chat_allowed_groups,
                blocked_users=config.dispatch_chat_blocked_users,
                blocked_groups=config.dispatch_chat_blocked_groups,
                allow_all=config.dispatch_chat_allow_all,
            )
            for bot_id in self._bots():
                results = await self._client().pending_jobs(bot_id, self._cursors.get(bot_id))
                for result in results:
                    if not policy.allows(user_id=result.sender_id, group_id=result.group_id):
                        continue
                    request_id = f"ai-job-{result.id}"
                    await self._queue().enqueue(
                        ConversationJob(
                            jobId=request_id,
                            requestId=request_id,
                            createdAt=time.time(),
                            sessionId=result.session_id,
                            botId=result.bot_id,
                            scene=result.scene,
                            groupId=result.group_id,
                            userId=result.sender_id,
                            prompt=result.prompt,
                            aiJobId=result.id,
                            aiJobReply=PersistedGeneratedReply(
                                text=result.result, requestId=request_id
                            ),
                            mentionTargetId=result.sender_id if result.scene == "group" else None,
                        )
                    )
                # 遍历所有待消费页，避免已入队或被禁用的目标长期挡住后面的结果。
                self._cursors[bot_id] = results[-1].id if results else None
