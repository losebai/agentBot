"""逐群串行的消息触发自动发言协调器。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

from agent_bot.group_context import RecentGroupContextStoreProtocol
from agent_bot.integrations.onebot.auto_speak import AutoSpeakBot, AutoSpeakService


class MessageAutoSpeakService(Protocol):
    """逐消息协调器依赖的不可变自动发言运行时。"""

    def accepts_message(self, bot_id: str, group_id: str) -> bool:
        """Return whether the runtime accepts this group message."""

        ...

    async def handle_group_message(
        self,
        bot: AutoSpeakBot,
        group_id: str,
        recent_group_context: str | None,
    ) -> bool:
        """Evaluate and optionally send one group auto-speak message."""

        ...


ServiceProvider = Callable[[], MessageAutoSpeakService | AutoSpeakService | None]
ContextStoreProvider = Callable[[], RecentGroupContextStoreProtocol]
RecentTextRecorder = Callable[[Bot, GroupMessageEvent], Awaitable[None]]


class GroupAutoSpeakTrigger:
    """按 Bot 与群串行完成记录、上下文快照和一次自由发言判断。"""

    def __init__(
        self,
        *,
        service_provider: ServiceProvider,
        recent_context_store_provider: ContextStoreProvider,
        record_recent_group_text: RecentTextRecorder,
    ) -> None:
        """保存动态运行时入口和现有近期上下文边界。"""

        self._service_provider = service_provider
        self._recent_context_store_provider = recent_context_store_provider
        self._record_recent_group_text = record_recent_group_text
        self._group_locks: dict[tuple[str, str], asyncio.Lock] = {}

    async def handle(self, bot: Bot, event: GroupMessageEvent) -> None:
        """非目标仅记录；目标在群锁内记录后读取包含当前消息的快照。"""

        bot_id = str(bot.self_id)
        group_id = str(event.group_id)
        if str(event.user_id) == bot_id:
            return
        service = self._service_provider()
        if service is None or not service.accepts_message(bot_id, group_id):
            await self._record_recent_group_text(bot, event)
            return

        lock = self._group_locks.setdefault((bot_id, group_id), asyncio.Lock())
        async with lock:
            await self._record_recent_group_text(bot, event)
            context = await asyncio.to_thread(
                self._recent_context_store_provider().context,
                bot_id=bot_id,
                group_id=group_id,
            )
            await service.handle_group_message(bot, group_id, context)
