"""媒体资源按进程所有，热刷新只更换调用配置。"""

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from agent_bot.ai.database import query
from agent_bot.ai.errors import AiError
from agent_bot.database_pool import finish_database_write
from agent_bot.materials.store import MaterialStore
from agent_bot.media.config import CLEANUP_INTERVAL_SECONDS, MAX_GENERATIONS, MAX_UPLOADS
from agent_bot.media.contracts import MediaReadiness
from agent_bot.media.errors import MediaError
from agent_bot.media.files import MediaFiles
from agent_bot.media.records import MediaRunStore
from agent_bot.media.store import MediaStore, finish_cleanup


class MediaSlots:
    """容量满时立即拒绝，避免上传或生成请求无限排队占内存。"""

    def __init__(self, maximum):
        """名额属于进程而不是某个可热刷新的调用客户端。"""
        self.maximum, self.active, self.closed = maximum, 0, False
        self.idle = asyncio.Event()
        self.idle.set()

    @asynccontextmanager
    async def slot(self):
        """单事件循环内原子领取并在所有退出路径归还名额。"""
        if self.closed:
            raise MediaError("MEDIA_NOT_READY", status_code=503)
        if self.active >= self.maximum:
            raise MediaError("MEDIA_BUSY", status_code=429)
        self.active += 1
        self.idle.clear()
        try:
            yield
        finally:
            self.active -= 1
            if self.active == 0:
                self.idle.set()

    async def close(self):
        """拒绝新请求并等已有任务退出，文件资源随后才可关闭。"""
        self.closed = True
        await self.idle.wait()


class MediaResources:
    """统一管理目录锁、存储、运行记录和清理循环。"""

    def __init__(self, config, database, *, connect):
        """只装配对象，导入与构造不创建目录或访问数据库。"""
        self.config = config
        self.files = MediaFiles(config.directory)
        self.store = MediaStore(database, connect=connect, files=self.files)
        self.materials = MaterialStore(
            database, connect=connect, files=MediaFiles(config.directory / "materials"),
        )
        self.records = MediaRunStore(database, connect=connect)
        self.generations, self.uploads = MediaSlots(MAX_GENERATIONS), MediaSlots(MAX_UPLOADS)
        self.readiness = MediaReadiness()
        self._started, self._closed, self._cleaner = False, False, None

    async def start(self):
        """启动时检查一次；缺表与目录故障只关闭媒体能力。"""
        if self._started or self._closed:
            return
        self._started = True
        try:
            await self.materials.start()
        except (AiError, OSError):
            # 既有部署须手动升级；临时附件可继续工作，素材入口明确返回未初始化。
            logging.getLogger(__name__).warning(
                "持久素材不可用，请检查素材目录并显式执行 2026-10-17-material-memory 升级脚本"
            )
        initialized = False
        try:
            await self.store.run(
                lambda c: [
                    query(c, f"SELECT {columns} FROM {table} LIMIT 0")
                    for table, columns in (
                        ("agent_bot_media_asset", "id,status,storage_key,expires_at"),
                        ("agent_bot_media_run", "execution_id,status"),
                        (
                            "agent_bot_ai_model_media",
                            "model_id,vision_enabled,image_api_profile,audio_chat_enabled",
                        ),
                    )
                ]
            )
            initialized = True
            await finish_database_write(self.files.start)
            await self.store.reconcile(self.store.clock())
            self.readiness = MediaReadiness(initialized=True, storage_available=True)
            self._cleaner = asyncio.create_task(self._cleanup_loop())
        except (AiError, OSError) as error:
            self.files.close()
            code = (
                "MEDIA_NOT_INITIALIZED"
                if isinstance(error, AiError) and error.code == "AI_NOT_INITIALIZED"
                else "MEDIA_STORAGE_UNAVAILABLE"
            )
            self.readiness = MediaReadiness(initialized=initialized, code=code)
        except BaseException:
            self.files.close()
            raise

    def require_ready(self):
        """调用入口显式检查，健康查询不重复尝试初始化资源。"""
        if not self.readiness.storage_available:
            raise MediaError(self.readiness.code or "MEDIA_NOT_READY", status_code=503)

    async def _cleanup_loop(self):
        """一分钟一次；临时数据库故障保留文件并等待下一周期。"""
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            with suppress(AiError, OSError):
                await self.store.cleanup(self.store.clock())

    async def aclose(self):
        """先结束清理和活动请求，最后释放目录锁；可重复调用。"""
        if self._closed:
            return
        self._closed = True
        self.readiness = MediaReadiness(
            initialized=self.readiness.initialized, code="MEDIA_NOT_READY"
        )
        if self._cleaner is not None:
            self._cleaner.cancel()
            with suppress(asyncio.CancelledError):
                await self._cleaner
        await finish_cleanup(self._finish_close())

    async def _finish_close(self):
        """等待资源名额归还以后关闭目录，数据库池由外层随后关闭。"""
        await self.generations.close()
        await self.uploads.close()
        await self.store.aclose()
        await self.materials.aclose()
