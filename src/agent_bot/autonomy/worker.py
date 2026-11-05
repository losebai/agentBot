"""独立持续智能体调度：唤醒、读事件、判定、执行一步、记录，然后休眠。"""

import asyncio
import logging

from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.providers.langchain import LangChainProvider
from agent_bot.ai.service import AiService
from agent_bot.ai.sessions import AiSessionStore
from agent_bot.autonomy.service import AutonomyService
from agent_bot.autonomy.store import AutonomyStore
from agent_bot.bootstrap import database_params, load_bootstrap_config, load_startup_settings
from agent_bot.database_pool import create_database_pool, finish_database_write
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.settings import SettingsStore

logger = logging.getLogger(__name__)


async def serve(
    *, once: bool = False, ready: asyncio.Event | None = None, local_only: bool = False,
) -> None:
    """暂停和权威配置读取失败时不行动，退出先结束在途步骤再关闭模型与连接池。"""
    bootstrap = load_bootstrap_config()
    params = database_params(bootstrap)
    stored = await finish_database_write(lambda: load_startup_settings(bootstrap))
    pool = create_database_pool(params)
    ai = None
    try:
        secret = stored.config.chat_session_secret.get_secret_value()
        store = AutonomyStore(params, connect=pool.connect, secret=secret, local_only=local_only)
        memories = TaggedMemoryStore(params, connect=pool.connect, secret=secret)
        settings = SettingsStore(params, connect=pool.connect, static_config=stored.config)
        ai = AiService(
            AiCatalog(params, connect=pool.connect),
            AiSessionStore(params, connect=pool.connect),
            CredentialResolver(),
            {
                protocol: LangChainProvider(protocol)
                for protocol in ("openai-chat", "openai-responses", "ollama")
            },
            max_concurrent_requests=1,
            maximum_context_chars=34000,
        )
        service = AutonomyService(store, memories, ai)
        if ready is not None:
            ready.set()
        after = ""
        while True:
            delay = stored.config.chat_config_refresh_interval_seconds
            try:
                current = await finish_database_write(settings.load)
                delay = current.config.chat_config_refresh_interval_seconds
                if not current.config.chat_active_actions_paused:
                    profiles = await store.profiles(after=after, limit=20)
                    for profile in profiles:
                        if local_only and profile["owner"].get("channel") != "local":
                            continue
                        current = await finish_database_write(settings.load)
                        if current.config.chat_active_actions_paused:
                            break
                        if not profile["policy"]["paused"]:
                            # 每个身份最多一步；无任务时只有本地事务，不发起模型请求。
                            await service.run_once(profile["id"])
                    after = profiles[-1]["id"] if len(profiles) == 20 else ""
            except Exception as error:
                if once:
                    raise
                logger.error("持续智能体本轮失败：error=%s", type(error).__name__)
            if once:
                return
            await asyncio.sleep(delay)
    finally:
        try:
            if ai is not None:
                await ai.aclose()
        finally:
            await pool.aclose()
