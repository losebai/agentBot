"""内置插件只读状态接口，启停写入继续共用配置版本锁。"""

import asyncio

from fastapi import APIRouter

from agent_bot.admin.models import PluginInfo


def install_plugin_routes(router: APIRouter, runtime, settings) -> None:
    """安装到已有认证路由，插件状态不公开给未授权调用者。"""

    @router.get("/plugins", response_model=list[PluginInfo])
    async def plugins() -> list[PluginInfo]:
        """同一数据库快照的保存列表与进程状态分别展示。"""
        stored = await asyncio.to_thread(settings.store.load)
        return [
            PluginInfo(
                id=item.metadata.id,
                name=item.metadata.name,
                version=item.metadata.version,
                description=item.metadata.description,
                saved_enabled=item.saved_enabled,
                enabled=item.enabled,
                status=item.status,
                restart_required=item.restart_required,
            )
            for item in runtime.plugins.snapshots(stored.config.app_enabled_plugins)
        ]
