"""宿主持有公共运行时、配置刷新和任务生产者，插件仅借用资源。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from agent_bot.bootstrap import database_params, load_bootstrap_config
from agent_bot.config import RuntimeConfig
from agent_bot.core.lifecycle import RuntimeLeaseGate, settings_refresh_loop, start_background_task
from agent_bot.core.runtime import CoreRuntimeState, build_core_state, close_core_state
from agent_bot.database_pool import MySqlConnectionPool, create_database_pool
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.integrations.workflow_store import WorkflowStore
from agent_bot.jobs.runner import AiJobRunner
from agent_bot.knowledge.store import KnowledgeStore
from agent_bot.media.config import MediaConfig
from agent_bot.media.resources import MediaResources
from agent_bot.runtime_fields import RESTART_REQUIRED_FIELDS
from agent_bot.runtime_reload import SettingsReloader
from agent_bot.services.workflows import WorkflowCatalogImporter
from agent_bot.settings import (
    DatabaseParams,
    SettingsStore,
    VersionedSettings,
    runtime_config_to_plain_values,
)

if TYPE_CHECKING:
    from fastapi import FastAPI
    from nonebot.drivers import Driver

    from agent_bot.agent.tools.chat import ChatToolBridge
    from agent_bot.plugins.manager import PluginManager


class RuntimeRefreshParticipant(Protocol):
    """渠道刷新边界只交换快照，公共核心不依赖具体渠道类型。"""

    @property
    def state(self) -> Any:
        """返回当前渠道快照，供刷新失败时恢复。"""
        ...

    async def pause(self) -> None:
        """暂停需要随配置切换的后台生产者。"""
        ...

    def build(self, config: RuntimeConfig, core_state: CoreRuntimeState) -> Any:
        """构建借用候选公共客户端的渠道快照。"""
        ...

    def publish(self, state: Any) -> None:
        """发布或恢复渠道快照。"""
        ...

    async def close(self, state: Any) -> None:
        """仅释放渠道自己的快照资源。"""
        ...

    async def resume(self) -> None:
        """恢复当前有效快照的生产者。"""
        ...


def effective_runtime_config(
    candidate: RuntimeConfig,
    current: RuntimeConfig,
) -> tuple[RuntimeConfig, frozenset[str]]:
    """运行值热更新时保留连接和插件选择的当前进程值。"""
    pending = frozenset(
        name
        for name in RESTART_REQUIRED_FIELDS
        if getattr(candidate, name) != getattr(current, name)
    )
    values = runtime_config_to_plain_values(candidate)
    values.update({name: getattr(current, name) for name in pending})
    return RuntimeConfig.model_validate(values), pending


async def close_actions(actions: Sequence[Callable[[], Awaitable[None]]]) -> None:
    """逐项关闭所有资源，最后报告首个失败，取消也不能跳过其他释放。"""
    failure = None
    for action in actions:
        try:
            await action()
        except BaseException as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise failure


class ApplicationRuntime:
    """公共服务独立于平台运行，一次租约保护整组快照的原子切换。"""

    def __init__(
        self,
        stored: VersionedSettings,
        *,
        database: DatabaseParams | None = None,
        database_pool: MySqlConnectionPool | None = None,
        media_resources: MediaResources | None = None,
    ) -> None:
        """构造只建立无连接对象，HTTP 快照由异步启动创建。"""
        self._config = stored.config
        self.database_params = database or database_params(load_bootstrap_config())
        self.database_pool = database_pool or create_database_pool(
            self.database_params,
            max_size=stored.config.chat_db_pool_size,
            acquire_timeout_seconds=stored.config.chat_db_timeout_seconds,
            connect_timeout_seconds=stored.config.chat_db_timeout_seconds,
            read_timeout_seconds=stored.config.chat_db_timeout_seconds,
            write_timeout_seconds=stored.config.chat_db_timeout_seconds,
        )
        self.settings_store = SettingsStore(
            self.database_params, connect=self.database_pool.connect, static_config=stored.config
        )
        self.media_resources = media_resources or MediaResources(
            MediaConfig.from_environment(),
            self.database_params,
            connect=self.database_pool.connect,
        )
        self.workflow_store = WorkflowStore(
            self.database_params, connect=self.database_pool.connect
        )
        self.gate = RuntimeLeaseGate()
        # 存储按进程复用，构造不访问数据库；QQ 插件关闭时管理知识仍可使用。
        self.knowledge_store = KnowledgeStore(
            self.database_params, connect=self.database_pool.connect
        )
        self.tagged_memory_store = TaggedMemoryStore(
            self.database_params,
            connect=self.database_pool.connect,
            secret=stored.config.chat_session_secret.get_secret_value(),
        )
        self._state: CoreRuntimeState | None = None
        self._bridge: ChatToolBridge | None = None
        self._participant: RuntimeRefreshParticipant | None = None
        self._started = False
        self._closed = False
        self._stop_task: asyncio.Task[None] | None = None
        self._refresh_task = None
        self._job_task = None
        self._reloader = SettingsReloader(
            self.settings_store, initial_revision=stored.revision, apply=self._apply_settings
        )
        self.job_runner = AiJobRunner(
            config_provider=lambda: self.config,
            client_provider=lambda: self.state.ai_client,
            runtime_lease=self.lease,
        )

    @property
    def config(self) -> RuntimeConfig:
        """启动前提供配置，运行时提供最新有效快照。"""
        return self._state.config if self._state is not None else self._config

    @property
    def state(self) -> CoreRuntimeState:
        """服务必须完成启动后访问，不静默构造第二份客户端。"""
        if self._state is None:
            raise RuntimeError("公共运行时尚未启动")
        return self._state

    @property
    def revision(self) -> int:
        """已处理版本与需重启字段分别报告。"""
        return self._reloader.revision

    def lease(self) -> AbstractAsyncContextManager[None]:
        """管理请求和插件消息共享同一门闩。"""
        return self.gate.lease()

    def bind_chat_bridge(self, bridge: ChatToolBridge) -> None:
        """平台工具通过显式能力注入，构造新快照时继续使用同一桥。"""
        if self._bridge is not None:
            raise RuntimeError("chat bridge already bound")
        self._bridge = bridge
        if self._state is not None:
            self._state.ai_client.agent_service.tool_runtime.chat_bridge = bridge

    def bind_refresh_participant(self, participant: RuntimeRefreshParticipant) -> None:
        """QQ 适配器提供暂停和快照接口，核心不导入 QQ 消息模块。"""
        if self._participant is not None:
            raise RuntimeError("refresh participant already bound")
        self._participant = participant

    def _build(self, config: RuntimeConfig) -> CoreRuntimeState:
        """媒体和存储按进程共享，只有调用客户端随配置替换。"""
        return build_core_state(
            config,
            database=self.database_params,
            database_pool=self.database_pool,
            media_resources=self.media_resources,
            workflow_store=self.workflow_store,
            chat_bridge=self._bridge,
            knowledge_store=self.knowledge_store,
        )

    async def start(self) -> None:
        """公共媒体和工作流准备完成后再允许渠道插件启动。"""
        if self._started:
            return
        if self._closed:
            raise RuntimeError("公共运行时已关闭")
        self._state = self._build(self._config)
        await self.media_resources.start()
        if self.config.chat_local_workflow_enabled:
            # 已有部署不能因启用插件执行 ALTER；目录升级由离线迁移命令明确触发。
            await asyncio.to_thread(self.workflow_store.list_all, limit=1)
            directory = self.config.chat_local_workflow_directory
            if directory is not None:
                definitions = await asyncio.to_thread(
                    WorkflowCatalogImporter().scan,
                    Path(directory),
                )
                await asyncio.to_thread(self.workflow_store.upsert, definitions)
        self._started = True

    def start_producers(self) -> None:
        """所有插件启动成功后才开启轮询，避免使用部分启动的平台状态。"""
        self._refresh_task = start_background_task(
            self._refresh_task,
            lambda: settings_refresh_loop(lambda: self.config, self._reloader),
            name="agentbot-settings-refresh",
        )
        self._job_task = start_background_task(
            self._job_task, self.job_runner.run, name="agentbot-python-jobs"
        )

    async def refresh_once(self) -> bool:
        """管理与离线测试可显式触发同一刷新编排。"""
        return await self._reloader.refresh_once()

    async def _apply_settings(self, stored: VersionedSettings) -> None:
        """在共享门闩内原子发布公共和渠道快照，失败恢复原生产者。"""
        config, _ = effective_runtime_config(stored.config, self.config)
        if config == self.config:
            return
        replacement = self._build(config)
        participant = self._participant
        old_core = self.state
        old_qq: Any = participant.state if participant is not None else None
        new_qq = None
        published = False
        await self.gate.begin_refresh()
        try:
            if participant is not None:
                await participant.pause()
            await self.gate.wait_until_idle()
            if participant is not None:
                new_qq = participant.build(config, replacement)
                participant.publish(new_qq)
            self._state = replacement
            published = True
        finally:
            try:
                if not published:
                    if participant is not None:
                        participant.publish(old_qq)
                    await close_actions(
                        [
                            *([lambda: participant.close(new_qq)] if new_qq is not None else []),
                            lambda: close_core_state(replacement),
                        ]
                    )
                else:
                    await close_actions(
                        [
                            *(
                                [lambda: participant.close(old_qq)]
                                if participant is not None
                                else []
                            ),
                            lambda: close_core_state(old_core),
                        ]
                    )
            finally:
                await self.gate.end_refresh()
                if participant is not None:
                    await participant.resume()

    async def stop_producers(self) -> None:
        """取消刷新与推理生产者，等待它们释放在途租约。"""
        tasks = [task for task in (self._refresh_task, self._job_task) if task is not None]
        self._refresh_task = self._job_task = None
        for task in tasks:
            task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException) and not isinstance(result, asyncio.CancelledError):
                raise result

    async def stop(self) -> None:
        """共享唯一清理任务，调用者取消后仍等待释放完成再传播取消。"""
        if self._stop_task is None:
            self._stop_task = asyncio.create_task(self._stop_resources(), name="agentbot-core-stop")
        cancellation = None
        while not self._stop_task.done():
            try:
                await asyncio.shield(self._stop_task)
            except asyncio.CancelledError as error:
                # 取消属于调用者，不能向清理任务传播；否则会遗留目录锁和数据库池。
                cancellation = error
        self._stop_task.result()
        if cancellation is not None:
            raise cancellation

    async def _stop_resources(self) -> None:
        """媒体清理与数据库池最后关闭，部分启动同样必须释放。"""
        self._closed, self._started = True, False
        try:
            await self.stop_producers()
        finally:
            await self.gate.begin_refresh()
            try:
                await self.gate.wait_until_idle()
                state, self._state = self._state, None
                await close_actions(
                    [
                        *([lambda: close_core_state(state)] if state is not None else []),
                        self.media_resources.aclose,
                        self.database_pool.aclose,
                    ]
                )
            finally:
                await self.gate.end_refresh()

    async def is_ready(self) -> bool:
        """公共就绪不以 QQ 已连接或可选媒体模型可用为条件。"""
        return self._started and not self._closed


def assemble_runtime(
    stored: VersionedSettings,
    driver: Driver,
    app: FastAPI | None = None,
) -> tuple[ApplicationRuntime, PluginManager]:
    """两个入口共享装配，统一回调负责启动失败回收和正常退出。"""
    from agent_bot.bootstrap import validate_onebot_driver
    from agent_bot.plugins.catalog import BUILTIN_PLUGINS
    from agent_bot.plugins.contracts import PluginContext
    from agent_bot.plugins.manager import PluginManager

    if "qqbot" in stored.config.app_enabled_plugins:
        validate_onebot_driver(driver, stored.config)
    runtime = ApplicationRuntime(stored)
    plugins = PluginManager(BUILTIN_PLUGINS, stored.config.app_enabled_plugins)
    plugins.prepare(PluginContext(driver, app, runtime))

    async def stop() -> None:
        """先关生产者与渠道，再释放公共客户端和数据库。"""
        await close_actions([runtime.stop_producers, plugins.stop, runtime.stop])

    async def start() -> None:
        """失败和取消都在同一启动编排里回收，成功后才启动公共轮询。"""
        try:
            await runtime.start()
            await plugins.start()
            runtime.start_producers()
        except BaseException:
            try:
                await stop()
            except BaseException:
                pass
            raise

    driver.on_startup(start)
    driver.on_shutdown(stop)
    return runtime, plugins
