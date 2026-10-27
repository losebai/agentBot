"""B 站插件独立持有网络资源，未登录不影响宿主就绪。"""

from __future__ import annotations

from typing import TYPE_CHECKING

import httpx

from agent_bot.plugins.bilibili.client import BilibiliClient
from agent_bot.plugins.bilibili.service import BilibiliService
from agent_bot.plugins.bilibili.store import BilibiliStore
from agent_bot.plugins.catalog import BILIBILI_METADATA
from agent_bot.plugins.contracts import PluginContext

if TYPE_CHECKING:
    from agent_bot.admin.models import BotInfo
    from agent_bot.core.application import ApplicationRuntime


class BilibiliPlugin:
    """禁用时懒工厂不加载本模块，启用后仍不创建后台评论任务。"""

    metadata = BILIBILI_METADATA

    def __init__(self) -> None:
        """构造不联网、不读取数据库。"""
        self._runtime: ApplicationRuntime | None = None
        self._http: httpx.AsyncClient | None = None
        self.service: BilibiliService | None = None

    def prepare(self, context: PluginContext) -> None:
        """只借用宿主，不占用 QQ 的配置刷新参与者。"""
        self._runtime = context.runtime

    async def start(self) -> None:
        """校验显式迁移已完成，再创建本插件的 HTTP 客户端。"""
        if self._runtime is None:
            raise RuntimeError('Bilibili plugin not prepared')
        store = BilibiliStore(self._runtime.database_params,
                              connect=self._runtime.database_pool.connect)
        await store.validate()
        self._http = httpx.AsyncClient(timeout=30, follow_redirects=False, trust_env=False)
        self.service = BilibiliService(store, BilibiliClient(self._http))
        self._runtime.bilibili_service = self.service

    async def stop(self) -> None:
        """先撤销工具提供者，再关闭专属 HTTP，不清除已保存账号。"""
        if self._runtime is not None:
            self._runtime.bilibili_service = None
        self.service = None
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def is_ready(self) -> bool:
        """就绪仅检查插件本地资源，不触发平台登录检测。"""
        return self.service is not None

    def bots(self) -> list[BotInfo]:
        """B 站没有 OneBot 连接，不伪造 QQ 平台连接。"""
        return []
