"""聊天媒体生成用例。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from typing import TYPE_CHECKING

from agent_bot.content_classifier import ContentCategory
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import ChatContext, ChatMediaResponse, ChatMessage, MediaOutputType
from agent_bot.services.chat.contracts import ChatClient
from agent_bot.services.chat.prompting import ChatPromptBuilder
from agent_bot.services.chat.request_control import ChatRequestCoordinator
from agent_bot.state import ConversationContext, ConversationStoreProtocol

if TYPE_CHECKING:
    from agent_bot.ai.routing import RoutedModelGuard


class ChatMediaGenerator:
    """在统一请求约束下调用媒体模型。"""

    def __init__(
        self,
        *,
        client: ChatClient,
        store: ConversationStoreProtocol,
        prompt_builder: ChatPromptBuilder,
        request_coordinator: ChatRequestCoordinator,
        model_router: ContentModelRouter,
        model_guard: RoutedModelGuard | None,
        context_provider: Callable[..., Awaitable[ConversationContext]] | None = None,
    ) -> None:
        """初始化媒体依赖，对话音频与普通回复读取同一份已发送上下文。"""

        self._client = client
        self._store = store
        self._prompt_builder = prompt_builder
        self._request_coordinator = request_coordinator
        self._model_router = model_router
        self._model_guard = model_guard
        self._context_provider = context_provider

    async def generate(
        self,
        session_id: str,
        prompt: str,
        *,
        output_type: MediaOutputType,
        timeout_seconds: float,
        max_bytes: int,
        image_format: str | None,
        voice: str | None,
        audio_format: str | None,
        user: str,
        user_name: str | None,
        chat_context: ChatContext | None,
        recent_group_context: str | None,
        reject_busy: bool,
        consume_cooldown: bool,
        request_id: str | None,
    ) -> ChatMediaResponse:
        """构建媒体请求并返回本地服务的类型化响应。"""

        normalized = self._prompt_builder.normalize(prompt)
        current_prompt = (
            normalized
            if chat_context is not None
            else self._prompt_builder.current_user_prompt(normalized, user_name)
        )
        async with self._request_coordinator.request(
            session_id,
            reject_busy=reject_busy,
            consume_cooldown=consume_cooldown,
            media=True,
        ):
            messages: Sequence[ChatMessage] = ()
            media_prompt: str | None = normalized
            if output_type == "AUDIO":
                conversation_context = (
                    await self._context_provider(session_id, exclude_request_id=request_id)
                    if self._context_provider is not None
                    else await asyncio.to_thread(self._store.context, session_id)
                )
                messages = self._prompt_builder.messages(
                    current_prompt,
                    conversation_context=conversation_context,
                    recent_group_context=recent_group_context,
                )
                media_prompt = None
            category = (
                ContentCategory.IMAGE_GENERATION
                if output_type == "IMAGE"
                else ContentCategory.AUDIO_GENERATION
            )
            model_id = self._model_router.model_for(category)
            if self._model_guard is not None:
                # 媒体预检按输出类型检查模型能力。
                from agent_bot.ai.routing import RoutedModelGuard

                if isinstance(self._model_guard, RoutedModelGuard):
                    route = self._client.media_route(output_type)
                    capability = route.capability
                    await self._model_guard.ensure_enabled(model_id, capability=capability)
                else:
                    await self._model_guard.ensure_enabled(model_id)
            return await self._client.generate_media(
                model=model_id,
                session_id=session_id,
                prompt=media_prompt,
                messages=messages,
                request_id=request_id or f"qq-{uuid.uuid4().hex}",
                output_type=output_type,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
                image_format=image_format,
                voice=voice,
                audio_format=audio_format,
                user=user,
            )
