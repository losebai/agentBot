"""独立记忆维护进程：只访问权威配置和事实库，不装配模型或聊天渠道。"""

import asyncio
import logging
import time
from collections.abc import Callable

from agent_bot.bootstrap import database_params, load_bootstrap_config, load_startup_settings
from agent_bot.database_pool import create_database_pool, finish_database_write
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.settings import SettingsStore

logger = logging.getLogger(__name__)

MAINTENANCE_INTERVAL_SECONDS = 3600
MAINTENANCE_BATCH_SIZE = 500


class MemoryWorker:
    """用单调时钟限制整理频率；持久事务负责跨进程去重和中断恢复。"""

    def __init__(
        self, store: TaggedMemoryStore, *, clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """重启后立即检查，不从本地缓存推断已提交的事实或复盘状态。"""
        self._store = store
        self._clock = clock
        self._next_due = 0.0
        self._last_review_date: str | None = None

    async def run_cycle(self, settings: SettingsStore) -> float:
        """每轮先读取开关，暂停不写事实；数据库线程完成后才进入下一轮或退出。"""
        stored = await finish_database_write(settings.load)
        refresh = stored.config.chat_config_refresh_interval_seconds
        if not stored.config.chat_tagged_memory_enabled:
            self._next_due = 0.0
            return refresh
        remaining = self._next_due - self._clock()
        if remaining > 0:
            return min(refresh, remaining)
        expired = await finish_database_write(
            lambda: self._store.expire_due(limit=MAINTENANCE_BATCH_SIZE),
        )
        report = await finish_database_write(self._store.record_daily_review)
        # 失败不推进计时；重新检查已提交的到期状态不会再次生成版本。
        self._next_due = self._clock() + MAINTENANCE_INTERVAL_SECONDS
        if expired:
            logger.info("长期记忆到期整理完成：facts=%s", expired)
        if report["date"] != self._last_review_date:
            self._last_review_date = report["date"]
            logger.info(
                "长期记忆每日复盘：date=%s conflicts=%s expiring=%s stale_candidates=%s",
                report["date"], report["conflict_count"], report["expiring_count"],
                len(report["stale_candidates"]),
            )
        return min(refresh, MAINTENANCE_INTERVAL_SECONDS)


async def serve(*, once: bool = False) -> None:
    """复用启动升级和连接池；单轮失败向调用方报告，常驻失败休眠后重新读配置。"""
    bootstrap = load_bootstrap_config()
    params = database_params(bootstrap)
    stored = await finish_database_write(lambda: load_startup_settings(bootstrap))
    pool = create_database_pool(params)
    try:
        store = TaggedMemoryStore(
            params, secret=stored.config.chat_session_secret.get_secret_value(),
            connect=pool.connect,
        )
        worker = MemoryWorker(store)
        settings = SettingsStore(params, connect=pool.connect, static_config=stored.config)
        while True:
            try:
                delay = await worker.run_cycle(settings)
            except Exception as error:
                if once:
                    raise
                logger.error("记忆 Worker 本轮失败：error=%s", type(error).__name__)
                delay = stored.config.chat_config_refresh_interval_seconds
            if once:
                return
            await asyncio.sleep(delay)
    finally:
        await pool.aclose()
