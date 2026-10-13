"""NoneBot Matcher 的声明与优先级配置。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from nonebot import on_command, on_message, on_metaevent, on_notice
from nonebot.adapters.onebot.v11 import (
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    LifecycleMetaEvent,
    MessageEvent,
    PrivateMessageEvent,
)
from nonebot.matcher import Matcher
from nonebot.rule import Rule, is_type
from nonebot.typing import T_State

from agent_bot.config import RuntimeConfig

EventRule = Callable[[MessageEvent], Awaitable[bool]]
StatefulEventRule = Callable[[MessageEvent, T_State], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class PluginMatchers:
    """插件注册的全部 Matcher。"""

    group_member_lifecycle: type[Matcher]
    group_member_notice: type[Matcher]
    reset_command: type[Matcher]
    draw_command: type[Matcher]
    laya_command: type[Matcher]
    summary_command: type[Matcher]
    at_command: type[Matcher]
    naraka_command: type[Matcher]
    naraka_search_command: type[Matcher]
    keyword_at_reply: type[Matcher]
    natural_chat: type[Matcher]
    group_recent_context: type[Matcher]


def build_plugin_matchers(
    config: RuntimeConfig,
    *,
    is_authorized: EventRule,
    matches_keyword_reply: StatefulEventRule,
    targets_natural_chat: EventRule,
    accepts_natural_chat: EventRule,
    accepts_recent_group_context: EventRule,
) -> PluginMatchers:
    """根据一次启动配置集中创建 NoneBot Matcher。"""

    onebot_message = is_type(PrivateMessageEvent, GroupMessageEvent)
    authorized = Rule(is_authorized)
    keyword_rule = Rule(matches_keyword_reply)
    natural_target_rule = Rule(targets_natural_chat)
    natural_chat_rule = Rule(accepts_natural_chat)
    recent_context_rule = Rule(accepts_recent_group_context)
    return PluginMatchers(
        group_member_lifecycle=on_metaevent(
            rule=is_type(LifecycleMetaEvent),
            priority=config.chat_priority,
            block=False,
        ),
        group_member_notice=on_notice(
            rule=is_type(GroupIncreaseNoticeEvent, GroupDecreaseNoticeEvent),
            priority=config.chat_priority,
            block=False,
        ),
        reset_command=on_command(
            "new",
            aliases={"reset", "重置对话", "新对话"},
            rule=onebot_message & authorized,
            priority=config.chat_reset_priority,
            block=True,
        ),
        draw_command=on_command(
            "画图",
            rule=onebot_message & authorized,
            priority=config.chat_priority,
            block=True,
            force_whitespace=True,
        ),
        laya_command=on_command(
            "laya",
            rule=onebot_message & authorized,
            priority=config.chat_priority,
            block=True,
            force_whitespace=True,
        ),
        summary_command=on_command(
            "总结",
            aliases={"summary", "总结消息"},
            rule=onebot_message & authorized,
            priority=config.chat_priority,
            block=True,
            force_whitespace=True,
        ),
        at_command=on_command(
            "at",
            aliases={"艾特"},
            rule=onebot_message & authorized,
            priority=config.chat_priority,
            block=True,
        ),
        naraka_command=on_command(
            "永劫",
            aliases={"永劫无间", "yjwj", "永劫详情", "永劫对局", "yjxq"},
            rule=onebot_message & authorized,
            priority=config.chat_priority,
            block=True,
            force_whitespace=True,
        ),
        naraka_search_command=on_command(
            "永劫搜索",
            aliases={"永劫查人", "yjss"},
            rule=onebot_message & authorized,
            priority=config.chat_priority,
            block=True,
            force_whitespace=True,
        ),
        keyword_at_reply=on_message(
            rule=onebot_message & authorized & keyword_rule,
            priority=config.chat_priority + 1,
            block=True,
        ),
        natural_chat=on_message(
            rule=(
                onebot_message
                & authorized
                & natural_target_rule
                & natural_chat_rule
            ),
            priority=config.chat_priority + 2,
            block=True,
        ),
        group_recent_context=on_message(
            rule=onebot_message & authorized & recent_context_rule,
            priority=config.chat_priority + 3,
            block=False,
        ),
    )
