"""引用消息总结命令处理。"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import NoReturn

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent
from nonebot.matcher import Matcher

from agent_bot.ai.chat_errors import ChatAiError, ChatAiImageError
from agent_bot.config import RuntimeConfig
from agent_bot.integrations.onebot.queue_processor import generated_reply_from_model
from agent_bot.integrations.onebot.replies import OneBotReplyService
from agent_bot.model_guard import ModelNotAvailableError
from agent_bot.multimodal import (
    ImageRecognitionDisabledError,
    InvalidImageAttachmentError,
    InvalidVideoAttachmentError,
    TooManyImagesError,
    TooManyVideosError,
)
from agent_bot.quoted_summary import (
    ForwardMessageUnavailableError,
    QuotedMessageEmptyError,
    QuotedPromptLimitError,
    build_quoted_summary_input,
)
from agent_bot.services.chat import (
    ChatBusyError,
    ChatRateLimitedError,
    ChatService,
    PromptTooLongError,
)
from agent_bot.services.conversation_queue import ConversationQueueFullError
from agent_bot.services.replies import (
    CommandUsageFacts,
    FeatureUnavailableFacts,
    InvalidInputFacts,
    ReplyFacts,
    ReplyIntent,
    TaskFailedFacts,
)
from agent_bot.state import MessageCounter


class SummaryCommandHandler:
    """校验引用内容并发送一次性 AI 总结。"""

    def __init__(
        self,
        *,
        config_provider: Callable[[], RuntimeConfig],
        chat_service_provider: Callable[[], ChatService],
        message_counter_provider: Callable[[], MessageCounter],
        session_id_provider: Callable[[Bot, MessageEvent], str],
        llm_user_name_provider: Callable[[MessageEvent], str | None],
        current_user_name_provider: Callable[[MessageEvent], str | None],
        reply_service: OneBotReplyService,
        reply_intent_factory: Callable[[Bot, MessageEvent, ReplyFacts], Awaitable[ReplyIntent]],
    ) -> None:
        """保存热更新依赖和共享回复服务。"""

        self._config_provider = config_provider
        self._chat_service_provider = chat_service_provider
        self._message_counter_provider = message_counter_provider
        self._session_id_provider = session_id_provider
        self._llm_user_name_provider = llm_user_name_provider
        self._current_user_name_provider = current_user_name_provider
        self._reply_service = reply_service
        self._reply_intent_factory = reply_intent_factory

    async def _finish_reply(
        self, matcher: type[Matcher], bot: Bot, event: MessageEvent, facts: ReplyFacts,
    ) -> NoReturn:
        """只用共享 Composer 表达确定的引用校验事实。"""

        await self._reply_service.finish_matcher(
            matcher, await self._reply_intent_factory(bot, event, facts),
        )

    async def handle(
        self, bot: Bot, event: MessageEvent, *, matcher: type[Matcher],
    ) -> None:
        """校验引用内容，已有模型总结仅适配来源一次而不再次生成。"""

        reply = event.reply
        if reply is None:
            await self._finish_reply(
                matcher, bot, event,
                CommandUsageFacts(command="/总结", usage="引用一条消息后发送 /总结"),
            )
        if not self._message_counter_provider().try_consume(str(event.user_id)):
            await matcher.finish()

        config = self._config_provider()
        sender_name = (
            getattr(reply.sender, "card", None)
            or getattr(reply.sender, "nickname", None)
            or str(reply.sender.user_id)
        )
        try:
            summary_input = await build_quoted_summary_input(
                bot, reply.message, quoted_sender=sender_name,
                image_recognition_enabled=config.chat_image_recognition_enabled,
                max_images=config.chat_max_images_per_message,
                max_prompt_chars=config.chat_max_input_chars,
            )
        except ImageRecognitionDisabledError:
            await self._finish_reply(
                matcher, bot, event, FeatureUnavailableFacts(feature="图片识别"),
            )
        except TooManyImagesError:
            await self._finish_reply(
                matcher, bot, event, InvalidInputFacts(
                    subject="引用图片",
                    requirement=f"最多 {config.chat_max_images_per_message} 张",
                ),
            )
        except InvalidImageAttachmentError:
            await self._finish_reply(
                matcher, bot, event,
                InvalidInputFacts(subject="图片地址", requirement="有效且未过期"),
            )
        except InvalidVideoAttachmentError:
            await self._finish_reply(
                matcher, bot, event,
                InvalidInputFacts(subject="视频地址", requirement="有效且未过期"),
            )
        except TooManyVideosError:
            await self._finish_reply(
                matcher, bot, event,
                InvalidInputFacts(subject="引用视频", requirement="最多 1 个"),
            )
        except ForwardMessageUnavailableError as exc:
            logger.warning("读取合并转发记录失败：{}", type(exc).__name__)
            await self._finish_reply(
                matcher, bot, event,
                TaskFailedFacts(operation="读取合并聊天记录", error_type=type(exc).__name__),
            )
        except QuotedMessageEmptyError:
            await self._finish_reply(
                matcher, bot, event,
                InvalidInputFacts(subject="引用消息", requirement="包含可总结的内容"),
            )
        except QuotedPromptLimitError:
            await self._finish_reply(
                matcher, bot, event, FeatureUnavailableFacts(feature="总结输入长度"),
            )

        request_id = f"qq-summary-{uuid.uuid4().hex}"
        try:
            answer = await self._chat_service_provider().ask_once(
                f"{self._session_id_provider(bot, event)}:quoted-summary",
                summary_input.prompt,
                system_prompt=config.chat_system_prompt,
                user=self._llm_user_name_provider(event),
                user_name=self._current_user_name_provider(event),
                image_urls=summary_input.image_urls,
                video_urls=summary_input.video_urls,
            )
        except PromptTooLongError:
            await self._finish_reply(
                matcher, bot, event, InvalidInputFacts(
                    subject="引用内容", requirement="长度不超过当前总结输入上限",
                ),
            )
        except (ChatBusyError, ChatRateLimitedError, ConversationQueueFullError):
            await matcher.finish()
        except ChatAiImageError:
            await self._finish_reply(
                matcher, bot, event,
                InvalidInputFacts(subject="引用图片", requirement="可下载且格式受支持"),
            )
        except (ChatAiError, ModelNotAvailableError) as exc:
            logger.warning("总结模型调用失败：{}", type(exc).__name__)
            await matcher.finish()

        if not answer.strip():
            await matcher.finish()
        generated = generated_reply_from_model(answer, request_id)
        await self._reply_service.gateway.finish_matcher(matcher, generated)
