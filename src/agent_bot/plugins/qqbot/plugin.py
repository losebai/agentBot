"""QQBot 生命周期适配，只有本插件持有 QQ 队列和平台后台任务。"""

from __future__ import annotations

import nonebot

from agent_bot.bootstrap import validate_onebot_driver
from agent_bot.plugins.catalog import QQBOT_METADATA
from agent_bot.plugins.contracts import PluginContext
from agent_bot.plugins.qqbot.binding import bind_context


class QQBotPlugin:
    """公共快照由宿主先启动，QQ 消息装配使用同一个公共客户端。"""

    metadata = QQBOT_METADATA

    def __init__(self) -> None:
        """构造不导入消息模块、不访问配置文件或队列。"""
        self._context: PluginContext | None = None
        self._module = None
        self._closed = False

    def prepare(self, context: PluginContext) -> None:
        """纯校验后注册适配器，队列与客户端保留到异步启动。"""
        from nonebot.adapters.onebot.v11 import Adapter

        validate_onebot_driver(context.driver, context.runtime.config)
        context.driver.register_adapter(Adapter)
        self._context = context
        bind_context(context)
        self._module = self._load_module()

    def _load_module(self):
        """沿用 NoneBot 正式加载，matcher 保持原插件所有权。"""
        if nonebot.load_plugin("agent_bot.plugin") is None:
            raise RuntimeError("failed to load QQBot message plugin")
        from agent_bot.plugin import application

        if application._context is not self._context:
            raise RuntimeError("QQBot message plugin belongs to another runtime")
        return application

    async def start(self) -> None:
        """宿主完成公共启动后分配 QQ 快照并逐项启动渠道资源。"""
        if self._context is None:
            raise RuntimeError("QQBot not prepared")
        self._module.initialize_channel_runtime()
        await self._module.initialize_chat_memory()
        await self._module.start_memory_cleanup()
        await self._module.start_chat_mcp()
        await self._module.start_conversation_queue()
        await self._module.start_auto_speak()
        await self._module.start_ai_job_polling()
        self._context.runtime.bind_refresh_participant(self)
        self._module.mark_channel_ready()

    async def stop(self) -> None:
        """部分启动也调用同一退出逻辑，绝不关闭公共池或媒体资源。"""
        if self._closed:
            return
        self._closed = True
        if self._module is not None:
            await self._module.close_clients()

    async def is_ready(self) -> bool:
        """队列和消费者健康即可，QQ 账号暂未连接不影响就绪。"""
        return bool(
            self._module is not None
            and not self._closed
            and await self._module.check_management_readiness()
        )

    def bots(self):
        """只观察 OneBot 本地连接。"""
        from nonebot.adapters.onebot.v11 import Bot

        from agent_bot.admin.models import BotInfo

        return [
            BotInfo(id=bot.self_id, adapter=bot.adapter.get_name())
            for bot in nonebot.get_bots().values()
            if isinstance(bot, Bot)
        ]

    @property
    def state(self):
        """热刷新通过明确的参与者接口访问 QQ 快照。"""
        return self._module.get_runtime_state()

    async def pause(self) -> None:
        """暂停自动发言，避免刷新期间提交新的消息。"""
        await self._module.stop_auto_speak()

    def build(self, config, core_state):
        """候选 QQ 快照借用同一候选公共快照。"""
        return self._module._build_runtime_state(config, self.state, core_state=core_state)

    def publish(self, state) -> None:
        """发布 QQ 兼容属性，同时同步适配器的名称配置。"""
        self._module._activate_runtime_state(state)

    async def close(self, state) -> None:
        """QQ 快照只释放聊天压缩和专属 HTTP 客户端。"""
        await self._module._close_runtime_state(state)

    async def resume(self) -> None:
        """刷新完成或失败恢复后重新按当前开关启动自动发言。"""
        await self._module.start_auto_speak()
