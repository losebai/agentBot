"""公共 Python 后台任务执行，不领取或确认 QQ 结果投递。"""

import asyncio
import logging

logger = logging.getLogger(__name__)


class AiJobRunner:
    """独立执行本地到期任务，不阻塞结果投递轮询。"""

    def __init__(self, *, config_provider, client_provider, runtime_lease):
        """执行全过程持有当前运行时，热更新等待在途推理完成。"""
        self._config = config_provider
        self._client = client_provider
        self._lease = runtime_lease

    async def run(self) -> None:
        """每轮至多领取一个任务，关闭时取消并由 Job 服务记录中断。"""
        while True:
            await asyncio.sleep(self._config().chat_job_poll_seconds)
            try:
                await self.run_once()
            except Exception as error:
                logger.warning("Python 任务执行失败：error=%s", type(error).__name__)

    async def run_once(self) -> None:
        """只在当前快照已启用 Job 时领取，防止旧配置越过热更新边界。"""
        async with self._lease():
            if self._config().chat_job_enabled:
                await self._client().run_due_jobs()
