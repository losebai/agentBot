"""ComfyUI 插件拥有按配置版本分配的客户端，渠道与管理 Agent 只借用。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from agent_bot.config import RuntimeConfig
from agent_bot.integrations.comfy import ComfyClient
from agent_bot.plugins.catalog import COMFY_METADATA
from agent_bot.plugins.comfy.launcher import ComfyLauncher
from agent_bot.plugins.contracts import PluginContext
from agent_bot.services.workflow_execution import WorkflowExecutionService
from agent_bot.services.workflows import WorkflowCatalogImporter

if TYPE_CHECKING:
    from agent_bot.core.application import ApplicationRuntime
    from agent_bot.integrations.workflow_store import WorkflowStore


class ComfyResources:
    """一代不可变借用关系；共享关闭任务保证重复释放与取消都不泄漏连接。"""

    def __init__(self, config: RuntimeConfig, store: WorkflowStore) -> None:
        """业务开关关闭时不分配客户端，但插件仍可管理工作流。"""
        self.client = ComfyClient(
            base_url=config.chat_local_workflow_base_url,
            timeout_seconds=config.chat_local_workflow_timeout_seconds,
            max_image_bytes=config.chat_image_max_bytes,
        ) if config.chat_local_workflow_enabled else None
        self.workflow_service = WorkflowExecutionService(store=store, client=self.client) \
            if self.client is not None else None
        self._close_task: asyncio.Task[None] | None = None

    async def aclose(self) -> None:
        """只关闭一次；调用者取消仍等 HTTP 连接释放后再传播取消。"""
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        cancellation = None
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError as error:
                cancellation = error
        self._close_task.result()
        if cancellation is not None:
            raise cancellation

    async def _close(self) -> None:
        """仅释放本代所有的 HTTP 客户端，不触碰宿主存储。"""
        if self.client is not None:
            await self.client.aclose()


class ComfyPlugin:
    """准备阶段登记工厂，启动同步目录，退出排空使用者后回收各代。"""

    metadata = COMFY_METADATA

    def __init__(self) -> None:
        """构造不连接数据库、不创建 HTTP 客户端。"""
        self._runtime: ApplicationRuntime | None = None
        self._resources: set[ComfyResources] = set()
        self._running = False
        self.launcher: ComfyLauncher | None = None

    def prepare(self, context: PluginContext) -> None:
        """先于公共快照登记工厂，禁用插件时不会经过此入口。"""
        self._runtime = context.runtime
        context.runtime.comfy_plugin = self
        self.launcher = ComfyLauncher(context.runtime.config)

    def build_resources(self, config: RuntimeConfig) -> ComfyResources:
        """客户端的配置版本与公共快照一致，不在请求中修改旧代。"""
        if self._runtime is None:
            raise RuntimeError("Comfy plugin not prepared")
        resources = ComfyResources(config, self._runtime.workflow_store)
        self._resources.add(resources)
        return resources

    async def release(self, resources: ComfyResources) -> None:
        """快照归还后移出所有权集合，长期热刷新不保留历史代。"""
        try:
            await resources.aclose()
        finally:
            self._resources.discard(resources)

    async def start(self) -> None:
        """只验证本地目录和数据库，不以远端 Comfy 服务在线作为就绪条件。"""
        if self._runtime is None:
            raise RuntimeError("Comfy plugin not prepared")
        store, config = self._runtime.workflow_store, self._runtime.config
        await asyncio.to_thread(store.list_all, limit=1)
        if config.chat_local_workflow_enabled and config.chat_local_workflow_directory is not None:
            definitions = await asyncio.to_thread(
                WorkflowCatalogImporter().scan, Path(config.chat_local_workflow_directory),
            )
            await asyncio.to_thread(store.upsert, definitions)
        self._running = True

    async def stop(self) -> None:
        """先撤销就绪，再排空租约；宿主仍可幂等归还当前快照。"""
        from agent_bot.core.application import close_actions
        from agent_bot.media.store import finish_cleanup

        self._running = False
        if self._runtime is None:
            return

        async def drain() -> None:
            """关闭期间阻止新请求借用即将回收的客户端。"""
            await self._runtime.gate.begin_refresh()
            await self._runtime.gate.wait_until_idle()
            await close_actions([lambda item=item: self.release(item)
                                 for item in tuple(self._resources)]
                                + ([self.launcher.aclose] if self.launcher is not None else []))
            # 插件只能随进程退出，门闩留给宿主最终关闭后再开放，避免借用已关闭的当前代。

        await finish_cleanup(drain())

    async def is_ready(self) -> bool:
        """就绪不执行网络探测。"""
        return self._running

    def bots(self) -> list:
        """Comfy 不提供消息平台连接。"""
        return []
