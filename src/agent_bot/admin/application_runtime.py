"""管理 API 观察宿主公共运行时，平台连接由插件管理器聚合。"""


class ApplicationRuntimeProvider:
    """始终读取当前快照，管理功能不依赖 QQ 模块全局变量。"""

    def __init__(self, runtime, plugins, *, startup_settings_store=None) -> None:
        """启动文件编辑能力在环境清理前由入口显式注入。"""
        self.runtime, self.plugins = runtime, plugins
        self.startup_settings_store = startup_settings_store

    @property
    def config(self):
        """有效配置包括保留的需重启字段。"""
        return self.runtime.config

    @property
    def revision(self):
        """返回唯一公共配置刷新器处理的版本。"""
        return self.runtime.revision

    @property
    def plugin_selection_locked(self) -> bool:
        """独立进程的插件由命令行固定，网页不能修改全局数据库选择。"""
        return self.runtime.standalone_plugin_id is not None

    @property
    def client(self):
        """客户端随快照发布替换。"""
        return self.runtime.state.ai_client

    @property
    def model_guard(self):
        """沿用当前目录模型校验。"""
        return self.runtime.state.model_guard

    @property
    def ai_service(self):
        """AI 服务始终来自公共客户端。"""
        return self.client.python_service

    @property
    def agent_service(self):
        """Agent 与 QQ 禁用状态独立。"""
        return self.client.agent_service

    @property
    def media_service(self):
        """媒体生成服务共用宿主资源。"""
        return self.client.media_service

    @property
    def media_resources(self):
        """进程媒体资源跨快照复用。"""
        return self.runtime.media_resources

    @property
    def media_store(self):
        """媒体文件访问使用既有租约和认证。"""
        return self.media_resources.store

    @property
    def workflow_store(self):
        """存储跨启停保留，只有当前运行的 Comfy 插件才授予 HTTP 管理能力。"""
        return self.runtime.workflow_store if self.plugins.running_plugin("comfy") else None

    def lease(self, *, read_only: bool = False):
        """短暂数据库观察不等长推理；执行及停机仍快速报告不可用。"""
        return self.runtime.lease(wait=False, read_only=read_only)

    @property
    def knowledge_store(self):
        """共享知识由宿主持有，禁用 QQ 插件时仍可管理。"""
        return self.runtime.knowledge_store

    @property
    def tagged_memory_store(self):
        """管理访问复用宿主数据库与启动会话密钥。"""
        return self.runtime.tagged_memory_store

    async def is_ready(self):
        """禁用平台不检查其队列，启用平台需完成启动。"""
        return await self.runtime.is_ready() and await self.plugins.is_ready()

    def bots(self):
        """本地连接来自已运行的插件。"""
        return self.plugins.bots()

    @property
    def queue_stores(self):
        """只暴露当前运行的 QQ 插件所持队列，禁用时无需导入消息模块。"""
        plugin = self.plugins.running_plugin("qqbot")
        return plugin.queue_stores() if plugin is not None else ()
