"""永劫无间查询命令及其 LLM 回复边界。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import NoReturn

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageEvent
from nonebot.matcher import Matcher

from agent_bot.integrations.onebot.replies import OneBotReplyService
from agent_bot.services.naraka import (
    NarakaBusyError,
    NarakaError,
    NarakaMatchDetail,
    NarakaPlayerSearchResult,
    NarakaQueryService,
    NarakaRateLimitedError,
    NarakaResult,
    is_naraka_match_id,
    is_naraka_role_id,
)
from agent_bot.services.replies import (
    CommandResultFacts,
    CommandUsageFacts,
    FeatureUnavailableFacts,
    ReplyFacts,
    ReplyIntent,
)


class NarakaCommandHandler:
    """查询确定性战绩事实，并由共享 Composer 完成唯一一次模型表述。"""

    def __init__(
        self,
        *,
        service_provider: Callable[[], NarakaQueryService | None],
        default_role_id_provider: Callable[[], str | None],
        session_id_provider: Callable[[Bot, MessageEvent], str],
        reply_service: OneBotReplyService,
        reply_intent_factory: Callable[[Bot, MessageEvent, ReplyFacts], Awaitable[ReplyIntent]],
        summary_formatter: Callable[[NarakaResult], str],
        match_formatter: Callable[[NarakaMatchDetail], str],
        player_search_formatter: Callable[[NarakaPlayerSearchResult], str],
    ) -> None:
        """保存热更新查询服务、事实序列化器与共享回复依赖。"""

        self._service_provider = service_provider
        self._default_role_id_provider = default_role_id_provider
        self._session_id_provider = session_id_provider
        self._reply_service = reply_service
        self._reply_intent_factory = reply_intent_factory
        self._summary_formatter = summary_formatter
        self._match_formatter = match_formatter
        self._player_search_formatter = player_search_formatter

    async def _finish_reply(
        self, matcher: type[Matcher], bot: Bot, event: MessageEvent, facts: ReplyFacts,
    ) -> NoReturn:
        """附上请求者身份，通过唯一 Composer 表达事实或静默结束。"""

        intent = await self._reply_intent_factory(bot, event, facts)
        intent = intent.model_copy(update={"user": str(event.user_id)})
        await self._reply_service.finish_matcher(matcher, intent)

    async def handle_query(
        self, bot: Bot, event: MessageEvent, argument: Message, *, matcher: type[Matcher],
    ) -> None:
        """查询角色或比赛，将格式化结果作为事实而非用户可见回复。"""

        raw_argument = argument.extract_plain_text().strip()
        target = raw_argument or self._default_role_id_provider()
        if (
            not target
            or (raw_argument and "." in target and not is_naraka_match_id(target))
            or (
                raw_argument and target.isascii() and target.isdigit()
                and not is_naraka_role_id(target)
            )
        ):
            await self._finish_reply(
                matcher, bot, event,
                CommandUsageFacts(command="/永劫", usage="/永劫 <角色ID、昵称或比赛ID>"),
            )
        service = self._service_provider()
        if service is None:
            await self._finish_reply(
                matcher, bot, event, FeatureUnavailableFacts(feature="永劫查询"),
            )

        try:
            session_id = self._session_id_provider(bot, event)
            if is_naraka_match_id(target):
                detail = await service.query_match_detail(session_id, target)
                content = self._match_formatter(detail)
            else:
                result = (
                    await service.query(session_id, target)
                    if not raw_argument or is_naraka_role_id(target)
                    else await service.query_by_nickname(session_id, target)
                )
                content = self._summary_formatter(result)
        except (NarakaError, NarakaBusyError, NarakaRateLimitedError, ValueError) as exc:
            logger.warning("永劫查询失败：{}", type(exc).__name__)
            await matcher.finish()

        await self._finish_reply(
            matcher, bot, event,
            CommandResultFacts(operation="查询永劫无间战绩", content=content),
        )

    async def handle_search(
        self, bot: Bot, event: MessageEvent, argument: Message, *, matcher: type[Matcher],
    ) -> None:
        """搜索角色并把保留完整数字和角色 ID 的结果交给 Composer。"""

        nickname = argument.extract_plain_text().strip()
        if not nickname:
            await self._finish_reply(
                matcher, bot, event,
                CommandUsageFacts(command="/永劫搜索", usage="/永劫搜索 <昵称>"),
            )
        service = self._service_provider()
        if service is None:
            await self._finish_reply(
                matcher, bot, event, FeatureUnavailableFacts(feature="永劫查询"),
            )

        try:
            result = await service.search_players(self._session_id_provider(bot, event), nickname)
            content = self._player_search_formatter(result)
        except (NarakaError, NarakaBusyError, NarakaRateLimitedError, ValueError) as exc:
            logger.warning("永劫角色搜索失败：{}", type(exc).__name__)
            await matcher.finish()

        await self._finish_reply(
            matcher, bot, event,
            CommandResultFacts(operation="搜索永劫无间角色", content=content),
        )
