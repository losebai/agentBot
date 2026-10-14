"""运行时租约与热更新切换编排。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress

from nonebot import logger

from agent_bot.config import RuntimeConfig
from agent_bot.plugin.runtime import RuntimeState
from agent_bot.runtime_reload import SettingsReloader
from agent_bot.settings import VersionedSettings

EffectiveConfigResolver = Callable[
    [RuntimeConfig, RuntimeConfig],
    tuple[RuntimeConfig, frozenset[str]],
]
StateBuilder = Callable[[RuntimeConfig, RuntimeState | None], RuntimeState]
AsyncAction = Callable[[], Awaitable[None]]
StateCloser = Callable[[RuntimeState], Awaitable[None]]


class RuntimeLeaseGate:
    """阻止热更新与正在处理的消息交叉使用不同运行时。"""

    def __init__(self) -> None:
        """创建无活跃使用者的运行时门闩。"""

        self._condition = asyncio.Condition()
        self._active_users = 0
        self._refreshing = False

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[None]:
        """等待刷新完成并登记一个运行时使用者。"""

        async with self._condition:
            while self._refreshing:
                await self._condition.wait()
            self._active_users += 1
        try:
            yield
        finally:
            async with self._condition:
                self._active_users -= 1
                if self._active_users == 0:
                    self._condition.notify_all()

    async def begin_refresh(self) -> None:
        """阻止新的运行时租约进入。"""

        async with self._condition:
            self._refreshing = True

    async def wait_until_idle(self) -> None:
        """等待所有已登记的运行时使用者退出。"""

        async with self._condition:
            while self._active_users:
                await self._condition.wait()

    async def end_refresh(self) -> None:
        """重新开放运行时租约。"""

        async with self._condition:
            self._refreshing = False
            self._condition.notify_all()


def start_background_task(
    current: asyncio.Task[None] | None,
    action: Callable[[], Awaitable[None]],
    *,
    name: str,
) -> asyncio.Task[None]:
    """幂等启动一个由插件持有的后台任务。"""

    if current is not None and not current.done():
        return current
    return asyncio.create_task(action(), name=name)


async def stop_background_task(
    task: asyncio.Task[None] | None,
    *,
    error_message: str,
) -> None:
    """取消后台任务，并只记录脱敏后的异常类型。"""

    if task is None:
        return
    task.cancel()
    try:
        with suppress(asyncio.CancelledError):
            await task
    except Exception as exc:
        logger.error(error_message, type(exc).__name__)


async def settings_refresh_loop(
    config_provider: Callable[[], RuntimeConfig],
    reloader: SettingsReloader,
) -> None:
    """按当前热更新间隔持续检查数据库设置 revision。"""

    while True:
        await asyncio.sleep(
            config_provider().chat_config_refresh_interval_seconds
        )
        try:
            await reloader.refresh_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            stage, error_type, reason = reloader.failure_details(exc)
            logger.warning(
                "配置自动刷新失败，继续使用上一版本：阶段={} 异常={} 原因={}",
                stage,
                error_type,
                reason,
            )


async def apply_runtime_settings(
    stored: VersionedSettings,
    *,
    current_state: RuntimeState,
    gate: RuntimeLeaseGate,
    effective_config_resolver: EffectiveConfigResolver,
    state_builder: StateBuilder,
    stop_auto_speak: AsyncAction,
    start_auto_speak: AsyncAction,
    state_publisher: Callable[[RuntimeState], None],
    state_closer: StateCloser,
    pending_fields_logger: Callable[[int, frozenset[str]], None],
) -> RuntimeState:
    """等待在途请求完成后原子切换一个完整运行时快照。"""

    effective_config, restart_fields = effective_config_resolver(
        stored.config,
        current_state.config,
    )
    if effective_config == current_state.config:
        logger.info("配置 revision={} 无需重建运行时。", stored.revision)
        pending_fields_logger(stored.revision, restart_fields)
        return current_state

    replacement_state = state_builder(effective_config, current_state)
    swapped = False
    await gate.begin_refresh()
    try:
        await stop_auto_speak()
        await gate.wait_until_idle()
        state_publisher(replacement_state)
        swapped = True
    finally:
        await gate.end_refresh()
        if not swapped:
            try:
                await state_closer(replacement_state)
            except Exception as exc:
                logger.warning("未发布配置资源清理失败：{}", type(exc).__name__)
            # 未发布新快照时恢复旧快照的后台任务，热刷新失败不能永久停掉自动发言。
            try:
                await start_auto_speak()
            except Exception as exc:
                logger.warning("恢复旧配置自动发言失败：{}", type(exc).__name__)

    try:
        await state_closer(current_state)
    except Exception as exc:
        logger.warning("旧配置资源清理失败：{}", type(exc).__name__)
    try:
        await start_auto_speak()
    except Exception as exc:
        logger.warning("刷新配置后启动自动发言失败：{}", type(exc).__name__)

    logger.info("配置 revision={} 已自动刷新。", stored.revision)
    pending_fields_logger(stored.revision, restart_fields)
    return replacement_state
