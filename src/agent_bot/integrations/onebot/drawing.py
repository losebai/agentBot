"""显式本地工作流画图命令处理。"""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable
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

from agent_bot.ai.chat_errors import ChatAiError
from agent_bot.ai.errors import AiError
from agent_bot.config import RuntimeConfig
from agent_bot.integrations.onebot.queue_processor import generated_reply_from_model
from agent_bot.integrations.onebot.replies import OneBotReplyService
from agent_bot.materials.contracts import MaterialView
from agent_bot.model_guard import ModelNotAvailableError
from agent_bot.models import ChatContext
from agent_bot.services.chat import (
    ChatBusyError,
    ChatRateLimitedError,
    ChatService,
    EmptyPromptError,
    PromptTooLongError,
)
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationQueue,
    ConversationQueueFullError,
    ConversationSessionBusyError,
)
from agent_bot.services.replies import (
    CommandUsageFacts,
    FeatureUnavailableFacts,
    InvalidInputFacts,
    ReplyFacts,
    ReplyIntent,
    TaskFailedFacts,
)
from agent_bot.services.workflow_execution import (
    WorkflowExecutionService,
    WorkflowImageRequest,
)
from agent_bot.state import MessageCounter

ChatContextProvider = Callable[[Bot, MessageEvent], Awaitable[ChatContext | None]]
MaterialRegistrar = Callable[
    [Bot, MessageEvent], Callable[[MessageSegment], Awaitable[MaterialView]] | None,
]


class DrawingCommandHandler:
    """先发送模型开画答复，再持久化提交后台绘图任务。"""

    def __init__(
        self,
        *,
        config_provider: Callable[[], RuntimeConfig],
        workflow_service_provider: Callable[[], WorkflowExecutionService | None],
        chat_service_provider: Callable[[], ChatService],
        queue_provider: Callable[[], ConversationQueue],
        image_queue_provider: Callable[[], ConversationQueue],
        message_counter_provider: Callable[[], MessageCounter],
        session_id_provider: Callable[[Bot, MessageEvent], str],
        llm_user_name_provider: Callable[[MessageEvent], str | None],
        current_user_name_provider: Callable[[MessageEvent], str | None],
        chat_context_provider: ChatContextProvider,
        reply_service: OneBotReplyService,
        reply_intent_factory: Callable[[Bot, MessageEvent, ReplyFacts], Awaitable[ReplyIntent]],
        material_registrar: MaterialRegistrar = lambda bot, event: None,
    ) -> None:
        """保存热更新依赖并复用应用共享的回复边界。"""

        self._config_provider = config_provider
        self._workflow_service_provider = workflow_service_provider
        self._chat_service_provider = chat_service_provider
        self._queue_provider = queue_provider
        self._image_queue_provider = image_queue_provider
        self._message_counter_provider = message_counter_provider
        self._session_id_provider = session_id_provider
        self._llm_user_name_provider = llm_user_name_provider
        self._current_user_name_provider = current_user_name_provider
        self._chat_context_provider = chat_context_provider
        self._reply_service = reply_service
        self._reply_intent_factory = reply_intent_factory
        self._material_registrar = material_registrar

    async def _finish_reply(
        self, matcher: type[Matcher], bot: Bot, event: MessageEvent, facts: ReplyFacts,
    ) -> NoReturn:
        """只通过共享 Composer 表达确定事实，生成失败时静默结束。"""

        await self._reply_service.finish_matcher(
            matcher, await self._reply_intent_factory(bot, event, facts),
        )

    async def handle(
        self, bot: Bot, event: MessageEvent, argument: Message, *, matcher: type[Matcher],
    ) -> None:
        """开画流程仅计入会话活动，保护重置但允许同群聊天并发。"""

        session_id = self._session_id_provider(bot, event)
        try:
            async with self._queue_provider().session_activity(session_id):
                await self._draw(
                    bot, event, argument, matcher=matcher, session_id=session_id,
                )
        except (ConversationSessionBusyError, ChatBusyError):
            await matcher.finish()

    async def _draw(
        self, bot: Bot, event: MessageEvent, argument: Message,
        *, matcher: type[Matcher], session_id: str,
    ) -> None:
        """发送前固定历史代际，后台结果仍可投递但不能写回已重置的新会话。"""

        prompt = argument.extract_plain_text().strip()
        usage = CommandUsageFacts(command="/画图", usage="/画图 <提示词>")
        if not prompt:
            await self._finish_reply(matcher, bot, event, usage)

        config = self._config_provider()
        workflow_service = self._workflow_service_provider()
        if not config.chat_local_workflow_enabled or workflow_service is None:
            await self._finish_reply(
                matcher, bot, event, FeatureUnavailableFacts(feature="本地绘图工作流"),
            )
        if not self._message_counter_provider().try_consume(str(event.user_id)):
            await matcher.finish()

        chat_service = self._chat_service_provider()
        try:
            normalized_prompt = chat_service.accept_immediate(session_id, prompt)
        except EmptyPromptError:
            await self._finish_reply(matcher, bot, event, usage)
        except PromptTooLongError:
            await self._finish_reply(
                matcher, bot, event, InvalidInputFacts(
                    subject="画图提示词",
                    requirement=f"长度不得超过 {config.chat_max_input_chars} 个字符",
                ),
            )
        except (ChatRateLimitedError, ChatBusyError, ConversationQueueFullError):
            await matcher.finish()

        register = self._material_registrar(bot, event)
        if register is not None:
            # 画图命令绕过自然聊天解析，只有授权、冷却和提示校验通过后接收附图。
            images = [segment for segment in argument if segment.type == "image"]
            try:
                if len(images) > 16:
                    raise ValueError("一次最多接收十六张图片")
                for image in images:
                    await register(image)
            except (
                AiError, OSError, ValueError, ActionFailed, ApiNotAvailable, NetworkError,
            ) as error:
                await self._finish_reply(
                    matcher, bot, event,
                    TaskFailedFacts(operation="保存素材", error_type=type(error).__name__),
                )

        request_id = f"qq-draw-{uuid.uuid4().hex}"
        generation = (
            await chat_service.history_generation(session_id)
            if self._reply_service.gateway.recorder is not None else None
        )
        chat_context = await self._chat_context_provider(bot, event)
        try:
            answer = await chat_service.generate_image_start_queued(
                session_id, normalized_prompt, request_id=request_id,
                user=self._llm_user_name_provider(event),
                user_name=self._current_user_name_provider(event),
                chat_context=chat_context,
            )
            generated = generated_reply_from_model(
                answer[:config.chat_max_reply_chars], request_id,
            )
        except (
            ChatAiError, ModelNotAvailableError, ChatBusyError, ChatRateLimitedError,
            ConversationQueueFullError, ValueError,
        ) as exc:
            logger.warning("直接画图的开画回复失败：{}", type(exc).__name__)
            await matcher.finish()
        await self._reply_service.gateway.send_matcher(
            matcher, generated,
            prefix=MessageSegment.at(event.user_id)
            if isinstance(event, GroupMessageEvent) else None,
        )
        task = ConversationJob(
            jobId=request_id, requestId=request_id, createdAt=time.time(),
            sessionId=session_id, botId=bot.self_id,
            scene="group" if isinstance(event, GroupMessageEvent) else "private",
            groupId=str(event.group_id) if isinstance(event, GroupMessageEvent) else None,
            userId=str(event.user_id), prompt=normalized_prompt,
            user=self._llm_user_name_provider(event),
            userName=self._current_user_name_provider(event), chatContext=chat_context,
            backgroundImage=True,
            historyGenerationId=generation,
            workflowRequest=WorkflowImageRequest(
                workflow_key=config.chat_local_workflow_default_key,
                positive_prompt=normalized_prompt,
            ),
        )
        try:
            await self._image_queue_provider().enqueue(task)
        except ConversationQueueFullError:
            await matcher.finish()
        except Exception as exc:
            logger.warning("绘图任务入队失败：{}", type(exc).__name__)
            await self._finish_reply(
                matcher, bot, event,
                TaskFailedFacts(operation="提交绘图任务", error_type=type(exc).__name__),
            )
        await chat_service.commit_queued(
            session_id, normalized_prompt, f"{generated.text}\n[绘图任务已提交]",
            chat_context=chat_context, request_id=request_id,
        )
        await matcher.finish()
