"""独立定时任务进程：读取权威配置、领取到期任务，空闲休眠且不连接 QQ。"""

import argparse
import asyncio
import json
import logging
from collections.abc import Sequence

from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.providers.langchain import LangChainProvider
from agent_bot.ai.service import AiService
from agent_bot.ai.sessions import AiSessionStore
from agent_bot.bootstrap import database_params, load_bootstrap_config, load_startup_settings
from agent_bot.database_pool import create_database_pool, finish_database_write
from agent_bot.jobs.service import JobService
from agent_bot.jobs.store import JobStore
from agent_bot.settings import SettingsStore

logger = logging.getLogger(__name__)


async def run_cycle(settings: SettingsStore, service: JobService) -> float:
    """领取前刷新开关、预算与全局指令；暂停不影响已在途调用的结果记录。"""
    stored = await finish_database_write(settings.load)
    if stored.config.chat_job_enabled and not stored.config.chat_active_actions_paused:
        service.ai_service.maximum_context_chars = stored.config.chat_llm_context_max_chars
        service.ai_service.providers["openai-responses"].system_prompt = (
            stored.config.chat_system_prompt
        )
        await service.run_due()
    return stored.config.chat_job_poll_seconds


async def serve(*, once: bool = False, inspect_id: str | None = None) -> None:
    """按数据库配置装配文本模型与全局指令，退出时关闭模型并释放连接池。"""
    params = database_params(load_bootstrap_config())
    stored = await finish_database_write(load_startup_settings)
    pool = create_database_pool(params)
    ai = None
    try:
        store = JobStore(params, connect=pool.connect)
        if inspect_id is not None:
            history = await store.inspect(inspect_id)
            print(json.dumps(history, ensure_ascii=False, default=str, indent=2))
            return
        ai = AiService(
            AiCatalog(params, connect=pool.connect),
            AiSessionStore(params, connect=pool.connect),
            CredentialResolver(),
            {protocol: LangChainProvider(protocol, system_prompt=stored.config.chat_system_prompt)
             for protocol in ("openai-chat", "openai-responses", "ollama")},
            max_concurrent_requests=1,
            maximum_context_chars=stored.config.chat_llm_context_max_chars,
        )
        service = JobService(store, ai)
        settings = SettingsStore(params, connect=pool.connect, static_config=stored.config)
        while True:
            try:
                delay = await run_cycle(settings, service)
            except Exception as error:
                if once:
                    raise
                # 配置读取失败时本轮不执行；不沿用旧的启用开关越过暂停边界。
                logger.error("Worker 本轮失败：error=%s", type(error).__name__)
                delay = stored.config.chat_job_poll_seconds
            if once:
                return
            await asyncio.sleep(delay)
    finally:
        try:
            if ai is not None:
                await ai.aclose()
        finally:
            await pool.aclose()


def run(argv: Sequence[str]) -> int:
    """提供任务或纯记忆维护模式，单轮检查与本机审计均不开放网页进程控制。"""
    parser = argparse.ArgumentParser(prog="agent-bot worker")
    kind = parser.add_mutually_exclusive_group()
    kind.add_argument("--memory-only", action="store_true", help="仅整理记忆和生成每日复盘")
    kind.add_argument(
        "--autonomy", action="store_true", help="执行持续身份的事件、计划和自我状态循环")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="仅运行一轮检查和执行")
    mode.add_argument("--inspect", metavar="JOB_ID", help="查看任务及执行审计，不调用模型")
    arguments = parser.parse_args(argv)
    if (arguments.memory_only or arguments.autonomy) and arguments.inspect is not None:
        parser.error("--memory-only/--autonomy cannot be combined with --inspect")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        if arguments.autonomy:
            from agent_bot.autonomy.worker import serve as serve_autonomy

            asyncio.run(serve_autonomy(once=arguments.once))
        elif arguments.memory_only:
            from agent_bot.memory.worker import serve as serve_memory

            asyncio.run(serve_memory(once=arguments.once))
        else:
            asyncio.run(serve(once=arguments.once, inspect_id=arguments.inspect))
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        logger.error("Worker 停止：error=%s", type(error).__name__)
        return 1
    return 0
