"""聊天消息解析、命令处理与队列入站编排。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import NoReturn

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable, NetworkError
from nonebot.matcher import Matcher

from agent_bot.ai.chat_errors import ChatAiRejectedError
from agent_bot.config import RuntimeConfig
from agent_bot.group_members import GroupMemberStoreError, GroupMemberValidationError
from agent_bot.integrations.onebot.replies import OneBotReplyService
from agent_bot.mentions import (
    AmbiguousMentionTargetError,
    InvalidGroupMemberDataError,
    InvalidMentionCommandError,
    InvalidMentionDirectiveError,
    MentionCommand,
    MentionTargetNotFoundError,
    NamedMentionRequest,
    build_mention_message,
    parse_conversational_mention,
    parse_mention_command,
    render_semantic_mentions,
    resolve_named_mention,
    resolve_tool_mention,
)
from agent_bot.model_guard import (
    ModelGuardUnavailableError,
    ModelNotAvailableError,
)
from agent_bot.models import ChatContext, ChatMediaResponse, ChatToolName
from agent_bot.multimodal import (
    MAX_VIDEOS_PER_MESSAGE,
    ImageRecognitionDisabledError,
    InvalidImageAttachmentError,
    InvalidVideoAttachmentError,
    MultimodalInput,
    TooManyImagesError,
    TooManyVideosError,
    parse_multimodal_input,
)
from agent_bot.quoted_summary import (
    ForwardMessageUnavailableError,
    QuotedMessageEmptyError,
    QuotedPromptLimitError,
    build_quoted_chat_input,
)
from agent_bot.services.chat import (
    ChatBusyError,
    ChatRateLimitedError,
    ChatService,
    EmptyPromptError,
    PromptTooLongError,
)
from agent_bot.services.chat.routing import ChatRouteSelector
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationJobExpiredError,
    ConversationQueue,
    ConversationQueueFullError,
    ConversationSessionBusyError,
)
from agent_bot.services.replies import (
    CommandResultFacts,
    CommandUsageFacts,
    FeatureUnavailableFacts,
    InvalidInputFacts,
    ReplyFacts,
    ReplyIntent,
    TaskFailedFacts,
)
from agent_bot.state import MessageCounter

ChatContextProvider = Callable[[Bot, MessageEvent], Awaitable[ChatContext]]
GroupMemberProvider = Callable[[Bot, int], Awaitable[object]]
MentionHandler = Callable[[Bot, GroupMessageEvent], Awaitable[Message | None]]


class ConversationHandler:
    """处理用户聊天输入并创建可恢复的持久化队列任务。"""

    def __init__(
        self,
        *,
        config_provider: Callable[[], RuntimeConfig],
        chat_service_provider: Callable[[], ChatService],
        message_counter_provider: Callable[[], MessageCounter],
        queue_provider: Callable[[], ConversationQueue],
        session_id_provider: Callable[[Bot, MessageEvent], str],
        llm_user_name_provider: Callable[[MessageEvent], str | None],
        current_user_name_provider: Callable[[MessageEvent], str | None],
        chat_context_provider: ChatContextProvider,
        recent_context_provider: Callable[[Bot, MessageEvent], Awaitable[str | None]],
        available_tools_provider: Callable[[MessageEvent], tuple[ChatToolName, ...]],
        group_members_loader: GroupMemberProvider,
        group_members_refresher: GroupMemberProvider,
        reply_service: OneBotReplyService,
    ) -> None:
        """保存聊天编排需要的热更新依赖。"""

        self._config_provider = config_provider
        self._chat_service_provider = chat_service_provider
        self._message_counter_provider = message_counter_provider
        self._queue_provider = queue_provider
        self._session_id_provider = session_id_provider
        self._llm_user_name_provider = llm_user_name_provider
        self._current_user_name_provider = current_user_name_provider
        self._chat_context_provider = chat_context_provider
        self._recent_context_provider = recent_context_provider
        self._available_tools_provider = available_tools_provider
        self._group_members_loader = group_members_loader
        self._group_members_refresher = group_members_refresher
        self._reply_service = reply_service

    async def reply_intent(
        self,
        bot: Bot,
        event: MessageEvent,
        facts: ReplyFacts,
    ) -> ReplyIntent:
        """为事件处理器构造共享会话身份和有界输入的类型化回复意图。"""

        prompt_limit = min(self._config_provider().chat_max_input_chars, 20_000)
        original_prompt = event.get_plaintext().strip() or "请回应用户"
        return ReplyIntent(
            requestId=f"qq-reply-{uuid.uuid4().hex}",
            sessionId=self._session_id_provider(bot, event),
            code=facts.kind,
            facts=facts,
            originalPrompt=original_prompt[:prompt_limit],
            user=self._llm_user_name_provider(event),
            userName=self._current_user_name_provider(event),
            chatContext=await self._chat_context_provider(bot, event),
        )

    async def _finish_reply(
        self,
        matcher: type[Matcher],
        bot: Bot,
        event: MessageEvent,
        facts: ReplyFacts,
        *,
        prefix: MessageSegment | None = None,
    ) -> NoReturn:
        """使用共享回复服务生成文字和可选真实 At，模型失败时静默结束。"""

        await self._reply_service.finish_matcher(
            matcher, await self.reply_intent(bot, event, facts), prefix=prefix,
        )

    async def deliver_media_answer(
        self,
        matcher: type[Matcher],
        response: ChatMediaResponse,
        *,
        reply_prefix: MessageSegment | None = None,
    ) -> str:
        """通过统一媒体网关发送图片或语音，并返回仅用于历史的文本占位。"""

        item = response.items[0]
        segment = (
            MessageSegment.image(f"base64://{item.data}")
            if item.type == "IMAGE"
            else MessageSegment.record(f"base64://{item.data}")
        )
        await self._reply_service.gateway.send_media_matcher(
            matcher, segment, prefix=reply_prefix,
        )
        if response.text is not None and response.text.strip():
            return response.text.strip()
        return "[已生成图片]" if item.type == "IMAGE" else "[已生成语音]"

    async def parse_chat_input(
        self,
        matcher: type[Matcher],
        message: Message,
        *,
        bot: Bot,
        event: MessageEvent,
    ) -> MultimodalInput:
        """解析多模态输入并通过回复模型解释确定的校验事实。"""

        config = self._config_provider()
        try:
            return parse_multimodal_input(
                message,
                enabled=config.chat_image_recognition_enabled,
                max_images=config.chat_max_images_per_message,
            )
        except ImageRecognitionDisabledError:
            await self._finish_reply(
                matcher, bot, event, FeatureUnavailableFacts(feature="图片识别"),
            )
        except TooManyImagesError:
            await self._finish_reply(
                matcher, bot, event, InvalidInputFacts(
                    subject="图片数量",
                    requirement=(
                        f"单条消息最多 {config.chat_max_images_per_message} 张图片"
                    ),
                ),
            )
        except InvalidImageAttachmentError:
            await self._finish_reply(
                matcher, bot, event, InvalidInputFacts(
                    subject="图片地址", requirement="提供有效且未过期的公开图片地址",
                ),
            )
        raise AssertionError("matcher.finish should stop handler execution")

    async def _render_group_mentions(
        self,
        bot: Bot,
        event: GroupMessageEvent,
        message: Message,
    ) -> Message:
        """把非指令原生 @ 转成群成员名称，必要时刷新成员目录一次。"""

        semantic_message = Message(
            [
                segment
                for segment in message
                if not (
                    segment.type == "at"
                    and str(segment.data.get("qq", "")).strip() == bot.self_id
                )
            ]
        )
        if not any(segment.type == "at" for segment in semantic_message):
            return semantic_message
        members = await self._group_members_loader(bot, event.group_id)
        try:
            return render_semantic_mentions(semantic_message, members)
        except MentionTargetNotFoundError:
            refreshed = await self._group_members_refresher(bot, event.group_id)
            return render_semantic_mentions(semantic_message, refreshed)

    async def reset(
        self,
        bot: Bot,
        event: MessageEvent,
        *,
        matcher: type[Matcher],
    ) -> None:
        """先重置当前会话历史，再由回复模型表达已确定的结果。"""

        try:
            session_id = self._session_id_provider(bot, event)
            async with self._queue_provider().session_guard(session_id, wait=False):
                await self._chat_service_provider().reset(session_id)
        except (ChatBusyError, ConversationSessionBusyError):
            await matcher.finish()
        await self._finish_reply(
            matcher, bot, event,
            CommandResultFacts(operation="重置当前会话", content="当前会话已重置"),
        )

    async def mention_command(
        self,
        bot: Bot,
        event: MessageEvent,
        argument: Message,
        *,
        matcher: type[Matcher],
    ) -> None:
        """确定性校验显式提醒目标，再发送真实 At 和模型生成正文。"""

        if not isinstance(event, GroupMessageEvent):
            await self._finish_reply(
                matcher, bot, event,
                InvalidInputFacts(subject="/at 使用场景", requirement="仅支持群聊使用"),
            )
        try:
            command = parse_mention_command(argument)
        except InvalidMentionCommandError:
            await self._finish_reply(
                matcher, bot, event,
                CommandUsageFacts(command="/at", usage="/at <QQ号或@成员> <内容>"),
            )
        counter = self._message_counter_provider()
        if not counter.try_consume(str(event.user_id)):
            await matcher.finish()
        try:
            members = await self._group_members_loader(bot, event.group_id)
            command = resolve_tool_mention(
                str(command.target_user_id), command.text, members, bot_user_id=bot.self_id,
            )
        except (MentionTargetNotFoundError, AmbiguousMentionTargetError):
            await self._finish_reply(
                matcher, bot, event,
                InvalidInputFacts(
                    subject="群成员",
                    requirement="没有找到唯一匹配成员，请补充可识别的 QQ 号、昵称或群名片",
                ),
            )
        except InvalidMentionDirectiveError:
            await self._finish_reply(
                matcher, bot, event,
                InvalidInputFacts(subject="群成员", requirement="请指定一名非机器人的群成员"),
            )
        except (
            ActionFailed, ApiNotAvailable, NetworkError, GroupMemberStoreError,
            InvalidGroupMemberDataError, GroupMemberValidationError,
        ) as exc:
            await self._finish_reply(
                matcher, bot, event,
                TaskFailedFacts(operation="读取群成员", error_type=type(exc).__name__),
            )
        await self._finish_reply(
            matcher, bot, event,
            CommandResultFacts(operation="提醒群成员", content=command.text),
            prefix=MessageSegment.at(command.target_user_id),
        )

    async def conversational_mention(
        self,
        bot: Bot,
        event: GroupMessageEvent,
        *,
        matcher: type[Matcher],
    ) -> Message | None:
        """识别自然语言中的单成员提醒，并解析昵称目标。"""

        if not self._config_provider().chat_auto_mention_enabled:
            return None
        try:
            directive = parse_conversational_mention(
                event.get_message(),
                bot_user_id=bot.self_id,
            )
        except InvalidMentionDirectiveError:
            counter = self._message_counter_provider()
            if not counter.try_consume(str(event.user_id)):
                await matcher.finish()
            await self._finish_reply(
                matcher, bot, event, InvalidInputFacts(
                    subject="群成员提醒", requirement="每次只能指定一名群成员",
                ),
            )
        if directive is None:
            return None
        counter = self._message_counter_provider()
        if not counter.try_consume(str(event.user_id)):
            await matcher.finish()

        command: MentionCommand
        if isinstance(directive, NamedMentionRequest):
            try:
                members = await self._group_members_loader(bot, event.group_id)
                refreshed = False
                if not members:
                    members = await self._group_members_refresher(bot, event.group_id)
                    refreshed = True
                try:
                    command = resolve_named_mention(
                        directive,
                        members,
                        bot_user_id=bot.self_id,
                    )
                except (AmbiguousMentionTargetError, MentionTargetNotFoundError):
                    if refreshed:
                        raise
                    members = await self._group_members_refresher(bot, event.group_id)
                    command = resolve_named_mention(
                        directive,
                        members,
                        bot_user_id=bot.self_id,
                    )
            except (ActionFailed, ApiNotAvailable, NetworkError) as exc:
                logger.warning("读取群成员列表失败：{}", type(exc).__name__)
                await self._finish_reply(
                    matcher, bot, event, TaskFailedFacts(
                        operation="读取群成员", error_type=type(exc).__name__,
                    ),
                )
            except AmbiguousMentionTargetError:
                await self._finish_reply(
                    matcher, bot, event, InvalidInputFacts(
                        subject="群成员名称", requirement="存在同名成员，需要直接选择并 @ 对方",
                    ),
                )
            except MentionTargetNotFoundError:
                return Message(event.get_message().extract_plain_text())
            except (InvalidGroupMemberDataError, GroupMemberValidationError) as exc:
                logger.warning("群成员列表返回异常：{}", type(exc).__name__)
                await self._finish_reply(
                    matcher, bot, event, TaskFailedFacts(
                        operation="解析群成员", error_type=type(exc).__name__,
                    ),
                )
        else:
            command = directive
        return build_mention_message(command)

    async def laya_command(
        self, bot: Bot, event: MessageEvent, argument: Message, *, matcher: type[Matcher],
    ) -> None:
        """通用 Laya 指令尚未实现，调用前明确说明功能不可用。"""
        await self._finish_reply(
            matcher, bot, event, FeatureUnavailableFacts(feature="通用 Laya 决策"),
        )

    async def natural_chat(
        self,
        bot: Bot,
        event: MessageEvent,
        *,
        matcher: type[Matcher],
        mention_handler: MentionHandler | None,
        message: Message | None = None,
        laya_decision: bool = False,
    ) -> None:
        """解析自然聊天消息，并将模型开画正文及当前配置分段一起写入持久队列。"""

        config = self._config_provider()
        recent_group_context = await self._recent_context_provider(bot, event)
        mention_message: Message | None = None
        if isinstance(event, GroupMessageEvent) and mention_handler is not None:
            mention_message = await mention_handler(bot, event)
        user_id = str(event.user_id)
        if mention_message is None and not self._message_counter_provider().try_consume(user_id):
            await matcher.finish()
        source_message = message if message is not None else self._restore_original_prefix(
            event,
            mention_message or event.get_message(),
        )
        if mention_message is None and isinstance(event, GroupMessageEvent):
            try:
                source_message = await self._render_group_mentions(
                    bot,
                    event,
                    source_message,
                )
            except (ActionFailed, ApiNotAvailable, NetworkError) as exc:
                logger.warning("读取被提及群成员失败：{}", type(exc).__name__)
                await self._finish_reply(
                    matcher, bot, event, TaskFailedFacts(
                        operation="读取被提及群成员", error_type=type(exc).__name__,
                    ),
                )
            except (
                InvalidGroupMemberDataError,
                GroupMemberValidationError,
                MentionTargetNotFoundError,
            ) as exc:
                logger.warning("被提及群成员数据异常：{}", type(exc).__name__)
                await self._finish_reply(
                    matcher, bot, event, InvalidInputFacts(
                        subject="被提及群成员", requirement="重新选择可识别的群成员",
                    ),
                )
        chat_input = await self.parse_chat_input(matcher, source_message, bot=bot, event=event)
        if event.reply is not None:
            sender_name = (
                getattr(event.reply.sender, "card", None)
                or getattr(event.reply.sender, "nickname", None)
                or str(event.reply.sender.user_id)
            )
            try:
                chat_input = await build_quoted_chat_input(
                    bot,
                    event.reply.message,
                    current_input=chat_input,
                    quoted_sender=sender_name,
                    image_recognition_enabled=config.chat_image_recognition_enabled,
                    max_images=config.chat_max_images_per_message,
                    max_videos=MAX_VIDEOS_PER_MESSAGE,
                    max_prompt_chars=config.chat_max_input_chars,
                )
            except ImageRecognitionDisabledError:
                await self._finish_reply(
                    matcher, bot, event, FeatureUnavailableFacts(feature="图片识别"),
                )
            except TooManyImagesError:
                await self._finish_reply(
                    matcher, bot, event, InvalidInputFacts(
                        subject="图片数量",
                        requirement=(
                            "当前消息与引用内容合计最多 "
                            f"{config.chat_max_images_per_message} 张图片"
                        ),
                    ),
                )
            except InvalidImageAttachmentError:
                await self._finish_reply(
                    matcher, bot, event, InvalidInputFacts(
                        subject="引用图片地址", requirement="提供有效且未过期的公开图片地址",
                    ),
                )
            except TooManyVideosError:
                await self._finish_reply(
                    matcher, bot, event, InvalidInputFacts(
                        subject="视频数量",
                        requirement=f"当前消息与引用内容合计最多 {MAX_VIDEOS_PER_MESSAGE} 个视频",
                    ),
                )
            except InvalidVideoAttachmentError:
                await self._finish_reply(
                    matcher, bot, event, InvalidInputFacts(
                        subject="引用视频地址", requirement="提供有效、未过期且大小符合限制的视频",
                    ),
                )
            except ForwardMessageUnavailableError as exc:
                logger.warning("读取引用的合并转发记录失败：{}", type(exc).__name__)
                await self._finish_reply(
                    matcher, bot, event, TaskFailedFacts(
                        operation="读取引用记录", error_type=type(exc).__name__,
                    ),
                )
            except QuotedMessageEmptyError:
                await self._finish_reply(
                    matcher, bot, event, InvalidInputFacts(
                        subject="引用内容", requirement="引用包含可识别内容的消息",
                    ),
                )
            except QuotedPromptLimitError:
                await self._finish_reply(
                    matcher, bot, event, InvalidInputFacts(
                        subject="消息长度",
                        requirement=(
                            "当前消息与引用内容合计最多 "
                            f"{config.chat_max_input_chars} 个字符"
                        ),
                    ),
                )
        if not chat_input.prompt:
            await self._finish_reply(
                matcher, bot, event, InvalidInputFacts(
                    subject="消息内容", requirement="回应用户的招呼或询问用户需要什么",
                ),
            )
        session_id = self._session_id_provider(bot, event)
        try:
            normalized_prompt = self._chat_service_provider().prepare_queued(
                chat_input.prompt
            )
        except EmptyPromptError:
            await self._finish_reply(
                matcher, bot, event, InvalidInputFacts(
                    subject="消息内容", requirement="回应用户的招呼或询问用户需要什么",
                ),
            )
        except PromptTooLongError:
            await self._finish_reply(
                matcher, bot, event, InvalidInputFacts(
                    subject="消息长度",
                    requirement=f"最多 {config.chat_max_input_chars} 个字符",
                ),
            )
        tools = () if laya_decision else ChatRouteSelector().select(
            chat_input.prompt,
            available_tools=self._available_tools_provider(event),
        ).chat_tools
        mention_target_id: str | None = None
        if mention_message is not None:
            raw_target = mention_message[0].data.get("qq")
            mention_target_id = str(raw_target) if raw_target is not None else None
        if mention_target_id is not None:
            # 原生 At 已替代提醒工具，预检必须使用最终入队的工具集合。
            tools = tuple(tool for tool in tools if tool != "mention_member")
        try:
            await self._chat_service_provider().validate_queued(
                normalized_prompt, image_urls=chat_input.image_urls,
                video_urls=chat_input.video_urls, chat_tools=tools,
            )
        except ModelGuardUnavailableError:
            logger.error("对话入队失败：模型目录暂时不可用，未创建任务")
            await self._reply_service.gateway.finish_configuration_error(matcher, kind="catalog")
        except ModelNotAvailableError:
            # 模型缺失时不能再调用回复模型来解释，配置错误直接返回给请求者。
            logger.error("对话入队失败：模型未配置或未启用，未创建任务")
            await self._reply_service.gateway.finish_configuration_error(matcher)
        except ChatAiRejectedError as exc:
            logger.error("对话入队配置无效：code={}，未创建任务", exc.error_code)
            await self._reply_service.gateway.finish_configuration_error(matcher, kind="agent")
        job_id = uuid.uuid4().hex
        request_id = f"qq-{job_id}"
        llm_user = self._llm_user_name_provider(event)
        user_name = self._current_user_name_provider(event)
        chat_context = await self._chat_context_provider(bot, event)
        job = ConversationJob(
            jobId=job_id,
            requestId=request_id,
            createdAt=time.time(),
            sessionId=session_id,
            botId=bot.self_id,
            scene="group" if isinstance(event, GroupMessageEvent) else "private",
            groupId=str(event.group_id) if isinstance(event, GroupMessageEvent) else None,
            userId=user_id,
            prompt=normalized_prompt,
            user=llm_user,
            userName=user_name,
            chatContext=chat_context,
            imageUrls=chat_input.image_urls,
            videoUrls=chat_input.video_urls,
            chatTools=tools,
            layaDecision=laya_decision,
            mentionTargetId=mention_target_id,
            recentGroupContext=recent_group_context,
        )
        try:
            await self._queue_provider().enqueue(job)
        except asyncio.CancelledError:
            raise
        except (
            ChatBusyError,
            ChatRateLimitedError,
            ConversationQueueFullError,
            ConversationJobExpiredError,
        ) as exc:
            logger.warning(
                "对话任务入队受限：job_id={} error={}",
                job.job_id,
                type(exc).__name__,
            )
            await matcher.finish()
        except Exception as exc:
            logger.warning(
                "对话任务写入 Redis 失败：job_id={} error={}",
                job.job_id,
                type(exc).__name__,
            )
            await self._finish_reply(
                matcher, bot, event, TaskFailedFacts(
                    operation="消息入队", error_type=type(exc).__name__,
                ),
            )
        await matcher.finish()

    def _restore_original_prefix(self, event: MessageEvent, message: Message) -> Message:
        """恢复 OneBot 在定向消息预处理时从纯文本开头移除的内容。"""

        original_text = event.original_message.extract_plain_text()
        adapter_text = event.get_message().extract_plain_text()
        if original_text == adapter_text or not original_text.endswith(adapter_text):
            return message
        prefix_length = len(original_text) - len(adapter_text)
        prefix = original_text[:prefix_length]
        if not prefix:
            return message
        restored = deepcopy(message)
        for segment in restored:
            if segment.type == "text":
                segment.data["text"] = prefix + segment.data["text"]
                return restored
        return message
