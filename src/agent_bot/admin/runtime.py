"""通过既有管理边界观察当前插件运行时，所有服务读取都在租约内进行。"""

import nonebot

from agent_bot.admin.models import BotInfo
from agent_bot.startup_settings import StartupSettingsStore


class PluginRuntimeProvider:
    """始终解析当前运行时，避免保留热刷新前的服务快照。"""

    def __init__(
        self, plugin, *, startup_settings_store: StartupSettingsStore | None = None,
    ) -> None:
        """复用已装配插件，应用入口在环境清理前注入启动文件能力。"""
        self.plugin = plugin
        self.startup_settings_store = startup_settings_store

    @property
    def config(self):
        """Return the effective live configuration, including retained restart fields."""
        return self.plugin.get_runtime_state().config

    @property
    def revision(self) -> int:
        """Return the latest revision successfully considered by the hot reloader."""
        return self.plugin.get_applied_settings_revision()

    @property
    def client(self):
        """Resolve the live AI client while the caller holds a runtime lease."""
        return self.plugin.get_runtime_state().ai_client

    @property
    def model_guard(self):
        """Use the existing enabled-model catalog validation."""
        return self.plugin.get_runtime_state().model_guard

    @property
    def ai_service(self):
        """Resolve the current Python service under the caller's runtime lease."""
        return self.plugin.get_runtime_state().ai_client.python_service

    def lease(self, *, read_only: bool = False):
        """旧入口同样允许排空时的数据库观察，执行仍保持快照保护。"""
        return self.plugin.acquire_runtime(wait=False, read_only=read_only)

    @property
    def queue_stores(self):
        """兼容入口只借用已经装配的两个队列，不从环境另建存储。"""
        queues = (self.plugin.conversation_queue, self.plugin.image_queue)
        if any(queue is None for queue in queues):
            return ()
        return tuple(queue.store for queue in queues)

    @property
    def agent_service(self):
        """从当前快照获取 Agent，避免缓存热刷新前的客户端。"""
        return self.plugin.get_runtime_state().ai_client.agent_service

    @property
    def media_service(self):
        """直接媒体服务使用当前运行时配置和独立模型连接。"""
        return self.plugin.get_runtime_state().ai_client.media_service

    @property
    def media_resources(self):
        """媒体存储和并发名额跨运行时快照共享。"""
        return self.plugin._media_resources

    @property
    def media_store(self):
        """管理 API 访问进程所有的文件存储。"""
        return self.media_resources.store

    @property
    def workflow_store(self):
        """工作流目录跨热刷新共享，只复用进程已装配的存储。"""
        return self.plugin._workflow_store

    async def is_ready(self) -> bool:
        """检查所选持久队列与消费者，不要求 QQ 或可选 Redis 已连接。"""
        return await self.plugin.check_management_readiness()

    @property
    def knowledge_store(self):
        """旧插件入口使用当前公共 Agent 的知识存储。"""
        return self.agent_service.tool_runtime.knowledge_store

    @property
    def tagged_memory_store(self):
        """旧插件管理入口复用已绑定启动密钥的记忆存储。"""
        return self.plugin._tagged_memory_store

    def bots(self) -> list[BotInfo]:
        """Read local adapter connections without causing platform side effects."""
        return [
            BotInfo(id=bot.self_id, adapter=bot.adapter.get_name())
            for bot in nonebot.get_bots().values()
        ]
