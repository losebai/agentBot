"""到期任务执行一次普通模型，生成与消息投递保持分离。"""

import asyncio
import logging

from agent_bot.ai.contracts import TextRequest
from agent_bot.media.store import finish_cleanup
from agent_bot.models import ChatMessage, LlmRequestOptions

logger = logging.getLogger(__name__)


class JobService:
    """每个运行时最多执行一个任务，跨实例执行权由数据库仲裁。"""

    def __init__(self, store, ai_service):
        """复用运行时拥有的普通模型客户端，不自行关闭共享连接。"""
        self.store, self.ai_service = store, ai_service
        self._lock = asyncio.Lock()

    async def run_due(self):
        """领取后提交事务再调用模型；明确失败不重试，中断由持久租约恢复。"""
        if self._lock.locked():
            return
        async with self._lock:
            job = await self.store.claim_due()
            if job is None:
                return
            logger.info("Job 开始执行：job=%s attempt=%s", job.id, job.lease_token)
            try:
                messages = [ChatMessage(role="user", content=job.prompt)]
                if job.system_prompt:
                    messages.insert(0, ChatMessage(role="system", content=job.system_prompt))
                request = TextRequest(
                    model=job.model,
                    session_id=job.session_id,
                    request_id=f"ai-job-{job.id}",
                    prompt=job.prompt,
                    messages=tuple(messages),
                    timeout_seconds=120,
                    options=LlmRequestOptions(max_tokens=4096),
                )
                result = await asyncio.wait_for(self.ai_service.complete(request), 120)
                if (
                    not isinstance(result.content, str)
                    or not result.content.strip()
                    or len(result.content) > 64000
                ):
                    raise ValueError("invalid job output")
            except asyncio.CancelledError:
                await finish_cleanup(self.store.finish(
                    job.id, lease_token=job.lease_token, error_code="EXECUTION_INTERRUPTED",
                ))
                raise
            except Exception as error:
                logger.warning("Job 模型执行失败：job=%s error=%s", job.id, type(error).__name__)
                await self.store.finish(
                    job.id, lease_token=job.lease_token, error_code="MODEL_EXECUTION_FAILED",
                )
            else:
                # 写回失败保留租约，过期后恢复；供应商不保证请求幂等，可能再次计费。
                accepted = await self.store.finish(
                    job.id, lease_token=job.lease_token, result=result.content,
                )
                logger.info("Job 结果提交：job=%s accepted=%s", job.id, accepted)

    async def pending_jobs(self, bot_id, after_id=None):
        """供现有机器人轮询器读取待投递成功结果。"""
        return await self.store.pending_results(bot_id, after_id)

    async def acknowledge_job(self, bot_id, job_id):
        """仅由持久消息队列确认投递完成后调用。"""
        await self.store.acknowledge(bot_id, job_id)
