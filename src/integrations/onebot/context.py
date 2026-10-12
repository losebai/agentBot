"""OneBot 事件身份、群资料与近期上下文服务。"""

from __future__ import annotations

import asyncio
import unicodedata
from collections.abc import Awaitable, Callable, Mapping, Sequence

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    MessageEvent,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable, NetworkError

from agent_bot.config import RuntimeConfig
from agent_bot.group_context import RecentGroupContextStoreProtocol
from agent_bot.group_members import (
    GroupMemberStore,
    GroupMemberStoreError,
    GroupMemberValidationError,
)
from agent_bot.models import ChatContext
from agent_bot.state import AccessPolicy, build_session_scope, external_session_id

_MAX_LLM_USER_NAME_UTF16_UNITS = 128
_MAX_SYNC_GROUPS = 1000
GroupMemberRefresher = Callable[..., Awaitable[object]]


class EventContextService:
    """集中处理事件身份、群成员目录和群聊近期文本。"""

    def __init__(
        self,
        *,
        config_provider: Callable[[], RuntimeConfig],
        access_policy_provider: Callable[[], AccessPolicy],
        session_secret_provider: Callable[[], str],
        group_member_store_provider: Callable[[], GroupMemberStore],
        recent_context_store_provider: Callable[[], RecentGroupContextStoreProtocol],
    ) -> None:
        """保存热更新运行时的延迟依赖。"""

        self._config_provider = config_provider
        self._access_policy_provider = access_policy_provider
        self._session_secret_provider = session_secret_provider
        self._group_member_store_provider = group_member_store_provider
        self._recent_context_store_provider = recent_context_store_provider

    def group_id(self, event: MessageEvent) -> str | None:
        """返回群聊事件的群号，私聊返回空值。"""

        if isinstance(event, GroupMessageEvent):
            return str(event.group_id)
        return None

    async def is_authorized(self, event: MessageEvent) -> bool:
        """根据当前访问策略校验消息来源。"""

        return self._access_policy_provider().allows(
            user_id=str(event.user_id),
            group_id=self.group_id(event),
        )

    def is_superuser(self, event: MessageEvent) -> bool:
        """判断消息发送者是否为当前运行时配置中的超级用户。"""

        return str(event.user_id) in self._config_provider().superusers

    def session_id(self, bot: Bot, event: MessageEvent) -> str:
        """生成不暴露 QQ 标识的稳定外部会话 ID。"""

        scope = build_session_scope(
            bot_id=bot.self_id,
            user_id=str(event.user_id),
            group_id=self.group_id(event),
            group_shared=self._config_provider().dispatch_chat_group_shared_session,
        )
        return external_session_id(scope, self._session_secret_provider())

    def current_user_name(self, event: MessageEvent) -> str | None:
        """返回适合当前轮身份上下文的 QQ 昵称。"""

        raw_card = getattr(event.sender, "card", None)
        raw_nickname = getattr(event.sender, "nickname", None)
        raw_name = (
            raw_card
            if isinstance(raw_card, str) and raw_card.strip()
            else raw_nickname
        )
        return self.normalize_context_name(raw_name)

    def normalize_context_name(self, value: object) -> str | None:
        """规范化昵称并按 UTF-16 单元限制长度。"""

        if not isinstance(value, str):
            return None
        sanitized = "".join(
            " " if character.isspace() else character
            for character in value[:1024]
            if character.isspace() or unicodedata.category(character) not in {"Cc", "Cs"}
        )
        normalized = " ".join(unicodedata.normalize("NFKC", sanitized).split())
        if not normalized:
            return None

        units = 0
        result: list[str] = []
        for character in normalized:
            character_units = 2 if ord(character) > 0xFFFF else 1
            if units + character_units > _MAX_LLM_USER_NAME_UTF16_UNITS:
                break
            result.append(character)
            units += character_units
        return "".join(result) or None

    async def resolve_group_name(self, bot: Bot, group_id: int) -> str | None:
        """读取并规范化当前群名称。"""

        try:
            group_info = await bot.get_group_info(group_id=group_id, no_cache=False)
        except (ActionFailed, ApiNotAvailable, NetworkError) as exc:
            logger.warning("读取当前群信息失败：{}", type(exc).__name__)
            return None
        if not isinstance(group_info, Mapping):
            logger.warning("当前群信息格式异常：{}", type(group_info).__name__)
            return None
        return self.normalize_context_name(group_info.get("group_name"))

    def chat_context(
        self,
        event: MessageEvent,
        *,
        group_name: str | None = None,
    ) -> ChatContext:
        """由已解析事件资料构建强类型聊天上下文。"""

        sender_name = self.current_user_name(event)
        bot_id = str(event.self_id) if self._config_provider().dispatch_chat_job_enabled else None
        if isinstance(event, GroupMessageEvent):
            return ChatContext(
                botId=bot_id,
                scene="group",
                groupId=str(event.group_id),
                groupName=group_name,
                senderId=str(event.user_id),
                senderName=sender_name,
            )
        return ChatContext(
            botId=bot_id,
            scene="private",
            senderId=str(event.user_id),
            senderName=sender_name,
        )

    async def refresh_group_members(
        self,
        bot: Bot,
        group_id: int,
        *,
        group_name: str | None,
    ) -> object:
        """从 OneBot 刷新单个群的成员目录。"""

        members = await bot.get_group_member_list(group_id=group_id, no_cache=False)
        try:
            return await self._group_member_store_provider().replace_group(
                bot_id=bot.self_id,
                group_id=str(group_id),
                group_name=group_name,
                members=members,
            )
        except GroupMemberStoreError as exc:
            logger.warning("保存群成员目录失败，继续使用实时成员数据：{}", type(exc).__name__)
            return members

    async def load_group_members(self, bot: Bot, group_id: int) -> object:
        """从持久化目录读取群成员。"""

        try:
            return await self._group_member_store_provider().list_members(
                bot.self_id,
                str(group_id),
            )
        except GroupMemberStoreError as exc:
            logger.warning("读取群成员目录失败，改用 NapCat 实时数据：{}", type(exc).__name__)
            return []

    async def sync_bot_group_members(
        self,
        bot: Bot,
        *,
        refresher: GroupMemberRefresher,
    ) -> None:
        """在机器人连接时同步其全部有效群成员目录。"""

        try:
            groups = await bot.get_group_list(no_cache=False)
        except (ActionFailed, ApiNotAvailable, NetworkError) as exc:
            logger.warning("读取机器人群列表失败：{}", type(exc).__name__)
            return
        if (
            not isinstance(groups, Sequence)
            or isinstance(groups, (str, bytes, bytearray))
            or len(groups) > _MAX_SYNC_GROUPS
        ):
            logger.warning("机器人群列表格式异常或超过 {} 个群", _MAX_SYNC_GROUPS)
            return

        for group in groups:
            if not isinstance(group, Mapping):
                logger.warning("机器人群列表包含无效记录")
                continue
            group_id = group.get("group_id")
            if type(group_id) is not int or group_id < 10_000:
                logger.warning("机器人群列表包含无效群号")
                continue
            try:
                await refresher(bot, group_id, group_name=group.get("group_name"))
            except (
                ActionFailed,
                ApiNotAvailable,
                NetworkError,
                GroupMemberValidationError,
            ) as exc:
                logger.warning("同步单个群成员目录失败：{}", type(exc).__name__)

    async def handle_group_member_notice(
        self,
        bot: Bot,
        event: GroupIncreaseNoticeEvent | GroupDecreaseNoticeEvent,
        *,
        refresher: GroupMemberRefresher,
    ) -> None:
        """按成员增减通知更新持久化目录。"""

        store = self._group_member_store_provider()
        if isinstance(event, GroupIncreaseNoticeEvent):
            if str(event.user_id) == bot.self_id:
                await refresher(bot, event.group_id)
                return
            member = await bot.get_group_member_info(
                group_id=event.group_id,
                user_id=event.user_id,
                no_cache=False,
            )
            await store.upsert_member(
                bot_id=bot.self_id,
                group_id=str(event.group_id),
                group_name=None,
                member=member,
            )
            return
        if str(event.user_id) == bot.self_id:
            await store.delete_group(bot.self_id, str(event.group_id))
        else:
            await store.delete_member(
                bot.self_id,
                str(event.group_id),
                str(event.user_id),
            )

    def llm_user_name(self, event: MessageEvent) -> str | None:
        """按隐私开关返回仅供当前上游请求使用的昵称。"""

        if not self._config_provider().dispatch_chat_llm_forward_user_name:
            return None
        return self.current_user_name(event)

    def recent_group_text(self, event: GroupMessageEvent) -> str | None:
        """提取可进入近期上下文的纯文本群消息。"""

        if event.reply is not None:
            return None
        message = event.get_message()
        if not message or any(segment.type != "text" for segment in message):
            return None
        content = " ".join(message.extract_plain_text().split())
        if not content:
            return None
        if any(
            prefix and content.startswith(prefix)
            for prefix in self._config_provider().command_start
        ):
            return None
        return content

    async def recent_group_context_for_request(
        self,
        bot: Bot,
        event: MessageEvent,
    ) -> str | None:
        """读取请求前的近期群聊上下文，并随后记录当前消息。"""

        if (
            not self._config_provider().dispatch_chat_group_recent_context_enabled
            or not isinstance(event, GroupMessageEvent)
        ):
            return None
        context = await asyncio.to_thread(
            self._recent_context_store_provider().context,
            bot_id=bot.self_id,
            group_id=str(event.group_id),
        )
        await self.record_recent_group_text(bot, event)
        return context

    async def record_recent_group_text(self, bot: Bot, event: GroupMessageEvent) -> None:
        """把有效的其他成员纯文本消息写入群聊近期上下文。"""

        if (
            not self._config_provider().dispatch_chat_group_recent_context_enabled
            or str(event.user_id) == bot.self_id
        ):
            return
        content = self.recent_group_text(event)
        if content is not None:
            await asyncio.to_thread(
                self._recent_context_store_provider().record,
                bot_id=bot.self_id,
                group_id=str(event.group_id),
                sender_id=str(event.user_id),
                sender_name=self.current_user_name(event),
                content=content,
            )
