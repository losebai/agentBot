"""应用插件的身份、状态和资源所有权契约。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from fastapi import FastAPI
    from nonebot.drivers import Driver

    from agent_bot.admin.models import BotInfo
    from agent_bot.core.application import ApplicationRuntime

PluginStatus = Literal["disabled", "prepared", "running", "stopped", "failed"]


@dataclass(frozen=True, slots=True)
class PluginMetadata:
    """供配置校验和管理界面读取的不可变插件身份。"""

    id: str
    name: str
    version: str
    description: str


@dataclass(frozen=True, slots=True)
class PluginContext:
    """宿主提供的共享资源，插件不得负责关闭公共资源。"""

    driver: Driver
    app: FastAPI | None
    runtime: ApplicationRuntime


class ApplicationPlugin(Protocol):
    """插件准备不联网，异步启动和停止由宿主统一编排。"""

    metadata: PluginMetadata

    def prepare(self, context: PluginContext) -> None:
        """注册接入并绑定共享上下文。"""
        ...

    async def start(self) -> None:
        """启动插件持有的资源与生产者。"""
        ...

    async def stop(self) -> None:
        """清理包括部分启动状态在内的插件资源。"""
        ...

    async def is_ready(self) -> bool:
        """检查插件本地服务状态，不要求外部平台账号在线。"""
        ...

    def bots(self) -> list[BotInfo]:
        """返回本地已观察到的平台连接。"""
        ...


@dataclass(frozen=True, slots=True)
class BuiltinPlugin:
    """元数据与懒工厂分开，禁用插件不会被实例化或导入。"""

    metadata: PluginMetadata
    factory: Callable[[], ApplicationPlugin]


@dataclass(frozen=True, slots=True)
class PluginSnapshot:
    """保存状态和进程状态分开，保存启停不伪装成已生效。"""

    metadata: PluginMetadata
    saved_enabled: bool
    enabled: bool
    status: PluginStatus
    restart_required: bool
