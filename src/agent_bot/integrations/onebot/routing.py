"""NoneBot 消息规则判断与关键词状态写入。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent
from nonebot.typing import T_State

from agent_bot.config import RuntimeConfig
from agent_bot.keyword_reply import find_keyword_reply
from agent_bot.message_routing import is_removed_chat_command


class MessageRouting:
    """集中管理聊天目标、命令排除与近期上下文规则。"""

    def __init__(
        self,
        *,
        config_provider: Callable[[], RuntimeConfig],
        recent_group_text_provider: Callable[[GroupMessageEvent], str | None],
    ) -> None:
        """保存热更新配置和群聊文本提取器。"""

        self._config_provider = config_provider
        self._recent_group_text_provider = recent_group_text_provider

    async def matches_keyword_reply(
        self,
        event: MessageEvent,
        state: T_State,
        *,
        state_key: str,
    ) -> bool:
        """匹配群聊关键词并把固定回复写入 matcher 状态。"""

        if not isinstance(event, GroupMessageEvent):
            return False
        reply = find_keyword_reply(
            event.get_message().extract_plain_text(),
            self._config_provider().chat_keyword_at_replies,
        )
        if reply is None:
            return False
        state[state_key] = reply
        return True

    async def targets_natural_chat(self, event: MessageEvent) -> bool:
        """判断消息是否明确指向当前机器人。"""

        if event.is_tome():
            return True
        if not isinstance(event, GroupMessageEvent):
            return False
        message = event.get_message().extract_plain_text().casefold()
        for nickname in self._config_provider().nickname:
            normalized_nickname = nickname.strip().casefold()
            if normalized_nickname and normalized_nickname in message:
                return True
        return False

    async def accepts_natural_chat(self, event: MessageEvent) -> bool:
        """排除已删除但仍可能被用户发送的 `/chat` 命令。"""

        config = self._config_provider()
        return not is_removed_chat_command(
            event.get_message().extract_plain_text(),
            config.command_start,
        )

    async def accepts_recent_group_context(
        self,
        event: MessageEvent,
        *,
        target_checker: Callable[[MessageEvent], Awaitable[bool]],
    ) -> bool:
        """筛选仅用于记录近期上下文的普通群聊文本。"""

        if (
            not self._config_provider().chat_group_recent_context_enabled
            or not isinstance(event, GroupMessageEvent)
            or await target_checker(event)
        ):
            return False
        return self._recent_group_text_provider(event) is not None
