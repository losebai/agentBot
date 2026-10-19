"""串行管理内置插件生命周期，失败时逆序回收所有已进入启动的资源。"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from agent_bot.plugins.contracts import (
    ApplicationPlugin,
    BuiltinPlugin,
    PluginContext,
    PluginSnapshot,
    PluginStatus,
)


class PluginLifecycleError(RuntimeError):
    """只公开插件 ID 和阶段，不把上游异常中的凭据带入管理日志。"""

    def __init__(self, plugin_id: str, stage: str) -> None:
        """底层错误通过异常链保留给调试者，安全消息保持固定结构。"""
        self.plugin_id, self.stage = plugin_id, stage
        super().__init__(f"plugin {plugin_id} {stage} failed")


class PluginManager:
    """注册顺序决定启动顺序，保存的新启用列表只用于状态展示。"""

    def __init__(self, catalog: Sequence[BuiltinPlugin], enabled: Sequence[str]) -> None:
        """在调用工厂前拒绝无效选择，避免任意模块加载。"""
        self._catalog = tuple(catalog)
        ids = {entry.metadata.id for entry in catalog}
        if len(ids) != len(catalog):
            raise ValueError("duplicate registered plugin ID")
        if len(set(enabled)) != len(enabled) or set(enabled) - ids:
            raise ValueError("unknown or duplicate enabled plugin ID")
        self._enabled = frozenset(enabled)
        self._plugins: dict[str, ApplicationPlugin] = {}
        self._states: dict[str, PluginStatus] = {name: "disabled" for name in ids}
        self._entered: list[str] = []
        self._stopped: set[str] = set()

    def prepare(self, context: PluginContext) -> None:
        """准备只做注册，不创建需要异步回收的连接。"""
        if self._plugins:
            raise RuntimeError("plugins already prepared")
        for entry in self._catalog:
            name = entry.metadata.id
            if name not in self._enabled:
                continue
            try:
                plugin = entry.factory()
                self._plugins[name] = plugin
                plugin.prepare(context)
                self._states[name] = "prepared"
            except Exception:
                self._states[name] = "failed"
                raise PluginLifecycleError(name, "prepare") from None

    async def start(self) -> None:
        """先登记启动中的插件，保证失败和取消也能释放部分资源。"""
        for name, plugin in self._plugins.items():
            if self._states[name] == "running":
                continue
            if self._states[name] != "prepared":
                raise PluginLifecycleError(name, "start")
            self._entered.append(name)
            try:
                await plugin.start()
                self._states[name] = "running"
            except BaseException as error:
                self._states[name] = "failed"
                try:
                    await self.stop()
                except BaseException:
                    # 启动异常是主要失败，清理已逐项执行，不用次要清理异常替换它。
                    pass
                self._states[name] = "failed"
                if isinstance(error, asyncio.CancelledError):
                    raise
                raise PluginLifecycleError(name, "start") from None

    async def stop(self) -> None:
        """清理失败仍继续其余资源，最后统一报告首个失败。"""
        failure: BaseException | None = None
        failed_name = ""
        for name in reversed(self._entered):
            if name in self._stopped:
                continue
            self._stopped.add(name)
            try:
                await self._plugins[name].stop()
                self._states[name] = "stopped"
            except BaseException as error:
                self._states[name] = "failed"
                if failure is None:
                    failure, failed_name = error, name
        if isinstance(failure, asyncio.CancelledError):
            raise failure
        if failure is not None:
            raise PluginLifecycleError(failed_name, "stop") from None

    async def is_ready(self) -> bool:
        """空选择天然满足插件就绪，已启用插件须全部成功启动。"""
        for name in self._enabled:
            if self._states[name] != "running" or not await self._plugins[name].is_ready():
                return False
        return True

    def bots(self):
        """聚合运行中插件的本地连接，不请求外部用户资料。"""
        return [
            bot
            for name, plugin in self._plugins.items()
            if self._states[name] == "running"
            for bot in plugin.bots()
        ]

    def running_plugin(self, plugin_id: str) -> ApplicationPlugin | None:
        """只提供已运行的插件实例，维护请求不能隐式加载或启动禁用插件。"""
        if self._states.get(plugin_id) != "running":
            return None
        return self._plugins[plugin_id]

    def snapshots(self, saved_enabled: Sequence[str]) -> list[PluginSnapshot]:
        """保存列表只影响待重启提示，不修改当前生命周期。"""
        saved = frozenset(saved_enabled)
        return [
            PluginSnapshot(
                entry.metadata,
                entry.metadata.id in saved,
                entry.metadata.id in self._enabled,
                self._states[entry.metadata.id],
                (entry.metadata.id in saved) != (entry.metadata.id in self._enabled),
            )
            for entry in self._catalog
        ]
