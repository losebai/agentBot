"""表情插件只持有独立图库，模型和凭据始终借用宿主公共资源。"""

from agent_bot.media.files import MediaFiles
from agent_bot.media.store import finish_cleanup
from agent_bot.plugins.catalog import STICKERS_METADATA
from agent_bot.plugins.stickers.service import StickerService
from agent_bot.plugins.stickers.store import StickerStore


class StickersPlugin:
    """默认停用，只有显式启用后创建图库目录并开放工具。"""

    metadata = STICKERS_METADATA

    def __init__(self) -> None:
        """构造不读取文件、不查询数据库或调用模型。"""
        self.runtime = None
        self.service: StickerService | None = None
        self.store: StickerStore | None = None

    def prepare(self, context) -> None:
        """只记录宿主，文件目录仍由启动配置确定。"""
        self.runtime = context.runtime

    async def start(self) -> None:
        """确认图库迁移及目录锁后发布服务，未配置模型仍可维护人工标签。"""
        if self.runtime is None:
            raise RuntimeError("Stickers plugin not prepared")
        self.store = StickerStore(
            self.runtime.database_params, connect=self.runtime.database_pool.connect,
            files=MediaFiles(self.runtime.media_resources.config.directory / "stickers"),
        )
        await self.store.start()
        self.service = StickerService(self.store, self.runtime)
        self.runtime.sticker_service = self.service

    async def stop(self) -> None:
        """先撤销新调用，再等待在途租约和文件事务完成，不能关闭共享模型客户端。"""
        if self.runtime is not None:
            self.runtime.sticker_service = None

        async def drain() -> None:
            """插件只随进程退出；门闩交给宿主最终关闭后再释放。"""
            if self.runtime is not None:
                await self.runtime.gate.begin_refresh()
                await self.runtime.gate.wait_until_idle()
            if self.service is not None:
                await self.service.uploads.close()
            if self.store is not None:
                await self.store.aclose()
            self.service = None

        await finish_cleanup(drain())

    async def is_ready(self) -> bool:
        """本地图库可用即为就绪，不在健康检查中产生付费视觉调用。"""
        return self.service is not None

    def bots(self) -> list:
        """插件借用现有QQ入口，不另建机器人连接。"""
        return []
