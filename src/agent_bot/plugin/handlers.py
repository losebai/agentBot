"""NoneBot Matcher 与应用处理器之间的事件绑定。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    LifecycleMetaEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable, NetworkError
from nonebot.params import CommandArg
from nonebot.typing import T_State

from agent_bot.ai.call_origin import command_scope
from agent_bot.group_members import GroupMemberStoreError, GroupMemberValidationError
from agent_bot.integrations.onebot.replies import OneBotReplyService
from agent_bot.memory_store import ChatMemoryStoreError
from agent_bot.plugin.matchers import PluginMatchers
from agent_bot.services.replies import (
    KeywordReplyFacts,
    PermissionDeniedFacts,
    ReplyFacts,
    ReplyIntent,
)

RuntimeLease = Callable[[], AbstractAsyncContextManager[None]]
AuthorizationChecker = Callable[[MessageEvent], Awaitable[bool]]
SuperuserChecker = Callable[[MessageEvent], bool]
BotEventHandler = Callable[[Bot, MessageEvent], Awaitable[None]]
CommandHandler = Callable[[Bot, MessageEvent, Message], Awaitable[None]]
MemberNoticeHandler = Callable[
    [Bot, GroupIncreaseNoticeEvent | GroupDecreaseNoticeEvent],
    Awaitable[None],
]


@dataclass(frozen=True, slots=True)
class PluginHandlerDependencies:
    """事件绑定需要的延迟应用回调。"""

    runtime_lease: RuntimeLease
    reply_service: OneBotReplyService
    reply_intent_factory: Callable[[Bot, MessageEvent, ReplyFacts], Awaitable[ReplyIntent]]
    is_authorized: AuthorizationChecker
    is_superuser: SuperuserChecker
    sync_group_members: Callable[[Bot], Awaitable[None]]
    handle_member_notice: MemberNoticeHandler
    handle_reset: BotEventHandler
    handle_draw: CommandHandler
    handle_laya: CommandHandler
    handle_mention: CommandHandler
    handle_natural_chat: BotEventHandler
    record_recent_group_text: Callable[[Bot, GroupMessageEvent], Awaitable[None]]
    handle_group_auto_speak: Callable[[Bot, GroupMessageEvent], Awaitable[None]]
    handle_summary: BotEventHandler
    handle_naraka: CommandHandler
    handle_naraka_search: CommandHandler
    keyword_state_key: str


@dataclass(frozen=True, slots=True)
class RegisteredPluginHandlers:
    """已挂载到 Matcher 的事件函数，供入口兼容导出。"""

    handle_group_member_lifecycle: Callable[..., Awaitable[None]]
    handle_group_member_notice: Callable[..., Awaitable[None]]
    handle_reset: Callable[..., Awaitable[None]]
    handle_draw_command: Callable[..., Awaitable[None]]
    handle_laya_command: Callable[..., Awaitable[None]]
    handle_at_command: Callable[..., Awaitable[None]]
    handle_natural_chat: Callable[..., Awaitable[None]]
    handle_group_recent_context: Callable[..., Awaitable[None]]
    handle_keyword_at_reply: Callable[..., Awaitable[None]]
    handle_summary_command: Callable[..., Awaitable[None]]
    handle_naraka_command: Callable[..., Awaitable[None]]
    handle_naraka_search_command: Callable[..., Awaitable[None]]


def register_plugin_handlers(
    matchers: PluginMatchers,
    dependencies: PluginHandlerDependencies,
) -> RegisteredPluginHandlers:
    """把薄事件适配函数注册到对应 Matcher。"""

    @matchers.group_member_lifecycle.handle()
    async def handle_group_member_lifecycle(
        bot: Bot,
        event: LifecycleMetaEvent,
    ) -> None:
        """在 OneBot 连接建立后同步群成员目录。"""

        if event.sub_type == "connect":
            await dependencies.sync_group_members(bot)

    @matchers.group_member_notice.handle()
    async def handle_group_member_notice(
        bot: Bot,
        event: GroupIncreaseNoticeEvent | GroupDecreaseNoticeEvent,
    ) -> None:
        """消费群成员增减通知并隔离目录更新异常。"""

        try:
            await dependencies.handle_member_notice(bot, event)
        except (
            ActionFailed,
            ApiNotAvailable,
            NetworkError,
            GroupMemberStoreError,
            GroupMemberValidationError,
        ) as exc:
            logger.warning("增量更新群成员目录失败：{}", type(exc).__name__)

    @matchers.reset_command.handle()
    async def handle_reset(bot: Bot, event: MessageEvent) -> None:
        """在运行时租约内处理会话重置命令。"""

        async with dependencies.runtime_lease(), command_scope():
            if not await dependencies.is_authorized(event):
                return
            if not dependencies.is_superuser(event):
                await dependencies.reply_service.finish_matcher(
                    matchers.reset_command,
                    await dependencies.reply_intent_factory(
                        bot, event, PermissionDeniedFacts(action="重置当前会话"),
                    ),
                )
            await dependencies.handle_reset(bot, event)

    @matchers.at_command.handle()
    async def handle_at_command(
        bot: Bot,
        event: MessageEvent,
        argument: Message = CommandArg(),
    ) -> None:
        """在运行时租约内处理显式群成员提醒命令。"""

        async with dependencies.runtime_lease(), command_scope():
            if await dependencies.is_authorized(event):
                await dependencies.handle_mention(bot, event, argument)

    @matchers.draw_command.handle()
    async def handle_draw_command(
        bot: Bot,
        event: MessageEvent,
        argument: Message = CommandArg(),
    ) -> None:
        """在运行时租约内处理默认本地工作流画图命令。"""

        async with dependencies.runtime_lease(), command_scope():
            if await dependencies.is_authorized(event):
                await dependencies.handle_draw(bot, event, argument)

    @matchers.laya_command.handle()
    async def handle_laya_command(
        bot: Bot, event: MessageEvent, argument: Message = CommandArg(),
    ) -> None:
        """在运行时租约内处理基于当前上下文的 Laya 决策命令。"""
        async with dependencies.runtime_lease(), command_scope():
            if await dependencies.is_authorized(event):
                await dependencies.handle_laya(bot, event, argument)

    @matchers.natural_chat.handle()
    async def handle_natural_chat(bot: Bot, event: MessageEvent) -> None:
        """在运行时租约内处理自然聊天消息。"""

        async with dependencies.runtime_lease():
            if await dependencies.is_authorized(event):
                await dependencies.handle_natural_chat(bot, event)

    @matchers.group_recent_context.handle()
    async def handle_group_recent_context(bot: Bot, event: MessageEvent) -> None:
        """记录未触发机器人回复的群聊近期文本。"""

        async with dependencies.runtime_lease():
            if await dependencies.is_authorized(event) and isinstance(
                event, GroupMessageEvent
            ):
                try:
                    await dependencies.handle_group_auto_speak(bot, event)
                except ChatMemoryStoreError as exc:
                    logger.warning("记录群聊近期文本失败：{}", type(exc).__name__)

    @matchers.keyword_at_reply.handle()
    async def handle_keyword_at_reply(
        bot: Bot,
        event: MessageEvent,
        state: T_State,
    ) -> None:
        """将命中配置作为模型事实，仅发送生成正文和真实发送者 At。"""

        async with dependencies.runtime_lease():
            if not await dependencies.is_authorized(event):
                return
            reply = state.get(dependencies.keyword_state_key)
            if not isinstance(reply, str):
                return
            if isinstance(event, GroupMessageEvent):
                try:
                    await dependencies.record_recent_group_text(bot, event)
                except ChatMemoryStoreError as exc:
                    logger.warning("记录群聊近期文本失败：{}", type(exc).__name__)
            await dependencies.reply_service.finish_matcher(
                matchers.keyword_at_reply,
                await dependencies.reply_intent_factory(
                    bot, event, KeywordReplyFacts(content=reply),
                ),
                prefix=MessageSegment.at(event.user_id),
            )

    @matchers.summary_command.handle()
    async def handle_summary_command(bot: Bot, event: MessageEvent) -> None:
        """在运行时租约内处理引用消息总结。"""

        async with dependencies.runtime_lease(), command_scope():
            if await dependencies.is_authorized(event):
                await dependencies.handle_summary(bot, event)

    @matchers.naraka_command.handle()
    async def handle_naraka_command(
        bot: Bot,
        event: MessageEvent,
        argument: Message = CommandArg(),
    ) -> None:
        """在运行时租约内处理永劫战绩或比赛查询。"""

        async with dependencies.runtime_lease(), command_scope():
            if await dependencies.is_authorized(event):
                await dependencies.handle_naraka(bot, event, argument)

    @matchers.naraka_search_command.handle()
    async def handle_naraka_search_command(
        bot: Bot,
        event: MessageEvent,
        argument: Message = CommandArg(),
    ) -> None:
        """在运行时租约内处理永劫角色搜索。"""

        async with dependencies.runtime_lease(), command_scope():
            if await dependencies.is_authorized(event):
                await dependencies.handle_naraka_search(bot, event, argument)

    return RegisteredPluginHandlers(
        handle_group_member_lifecycle=handle_group_member_lifecycle,
        handle_group_member_notice=handle_group_member_notice,
        handle_reset=handle_reset,
        handle_draw_command=handle_draw_command,
        handle_laya_command=handle_laya_command,
        handle_at_command=handle_at_command,
        handle_natural_chat=handle_natural_chat,
        handle_group_recent_context=handle_group_recent_context,
        handle_keyword_at_reply=handle_keyword_at_reply,
        handle_summary_command=handle_summary_command,
        handle_naraka_command=handle_naraka_command,
        handle_naraka_search_command=handle_naraka_search_command,
    )
