"""聊天会话用例编排。"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime
from typing import TYPE_CHECKING

from agent_bot.content_classifier import ContentCategory
from agent_bot.database_pool import finish_database_write
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import (
    ChatContext,
    ChatMediaResponse,
    ChatMessage,
    ChatToolName,
    MediaOutputType,
)
from agent_bot.sent_messages import SentMessageStore, merge_sent_messages
from agent_bot.services.chat.compression_queue import ChatCompressionQueue
from agent_bot.services.chat.contracts import (
    ChatClient,
    MediaReplyDelivery,
    ReplyDelivery,
)
from agent_bot.services.chat.media import ChatMediaGenerator
from agent_bot.services.chat.memory import ChatMemoryCompressor
from agent_bot.services.chat.prompting import ChatPromptBuilder
from agent_bot.services.chat.request_control import ChatRequestCoordinator
from agent_bot.services.chat.routing import ChatRouteSelector
from agent_bot.services.tool_images import has_tool_images
from agent_bot.session_delivery import SessionDeliveryStore
from agent_bot.state import ConversationContext, ConversationStoreProtocol

if TYPE_CHECKING:
    from agent_bot.ai.routing import RoutedModelGuard


class ChatService:
    """编排文本、媒体、会话历史和投递提交，不持有底层实现细节。"""

    def __init__(
        self,
        *,
        client: ChatClient,
        store: ConversationStoreProtocol,
        system_prompt: str,
        max_input_chars: int,
        cooldown_seconds: float,
        max_concurrent_requests: int,
        max_concurrent_media_requests: int = 1,
        max_rate_limit_entries: int = 1000,
        clock: Callable[[], float] = time.monotonic,
        model_guard: RoutedModelGuard | None = None,
        model_router: ContentModelRouter,
        web_search_enabled: bool = True,
        memory_compression_max_tokens: int = 512,
        memory_compression_queue_capacity: int = 64,
        sent_messages: SentMessageStore | None = None,
        session_delivery: SessionDeliveryStore | None = None,
    ) -> None:
        """生产通过统一上下文 facade 收发；旧日志参数仅服务于显式 legacy 调用。"""

        if max_input_chars <= 0 or max_concurrent_requests <= 0:
            raise ValueError("chat service limits must be positive")
        if (
            cooldown_seconds < 0
            or max_rate_limit_entries <= 0
            or memory_compression_max_tokens <= 0
        ):
            raise ValueError("cooldown limits are invalid")
        self.client = client
        self._store = store
        self._sent_messages = sent_messages
        self._session_delivery = session_delivery
        self._model_guard = model_guard
        self._model_router = model_router
        self._web_search_enabled = web_search_enabled
        self._route_selector = ChatRouteSelector()
        self._image_start_semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._prompt_builder = ChatPromptBuilder(
            system_prompt=system_prompt,
            max_input_chars=max_input_chars,
        )
        self._request_coordinator = ChatRequestCoordinator(
            cooldown_seconds=cooldown_seconds,
            max_concurrent_requests=max_concurrent_requests,
            max_concurrent_media_requests=max_concurrent_media_requests,
            max_rate_limit_entries=max_rate_limit_entries,
            clock=clock,
        )
        self._memory_compressor = ChatMemoryCompressor(
            client=client,
            store=store,
            model_router=model_router,
            max_tokens=memory_compression_max_tokens,
        )
        self._compression_queue = ChatCompressionQueue(
            self._memory_compressor.compress,
            capacity=memory_compression_queue_capacity,
        )
        self._media_generator = ChatMediaGenerator(
            client=client,
            store=store,
            prompt_builder=self._prompt_builder,
            request_coordinator=self._request_coordinator,
            model_router=model_router,
            model_guard=model_guard,
            context_provider=self.conversation_context,
        )

    async def history_generation(self, session_id: str) -> str:
        """发送前固定会话代际，后台任务结束时不能重建已被用户重置的上下文。"""
        return await finish_database_write(lambda: self._store.ensure_generation(session_id))

    async def conversation_context(
        self, session_id: str, *, exclude_request_id: str | None = None,
    ) -> ConversationContext:
        """生产只读统一消息流，恢复已确认回执后排除当前输入，不拼接旧出站表。"""
        if self._session_delivery is not None:
            return await self._session_delivery.context(
                session_id, exclude_request_id=exclude_request_id,
            )
        context = await asyncio.to_thread(self._store.context, session_id)
        if self._sent_messages is None or context.generation_id is None:
            return context
        if self._store.history_turns == 0:
            return context
        rows = await self._sent_messages.list_messages(session_id, context.generation_id)
        return merge_sent_messages(
            context, rows, max_chars=min(80_000, self._store.max_history_chars),
            max_message_chars=min(32_000, self._store.max_history_chars),
        )

    async def receive_queued(
        self, session_id: str, prompt: str, *, request_id: str,
        created_at: datetime | None = None, chat_context: ChatContext | None = None,
        generation_id: str | None = None,
    ) -> None:
        """在生成之前登记原用户输入，保留事件时间和代际；未注入 facade 的旧调用不变。"""
        if self._session_delivery is None:
            return
        normalized = self._prompt_builder.normalize(prompt)
        content = self._prompt_builder.history_user_prompt(normalized, chat_context)
        compression = await self._session_delivery.record_received(
            session_id, content, request_id=request_id, created_at=created_at,
            generation_id=generation_id,
        )
        if compression is not None:
            self._compression_queue.submit(compression)

    async def finish_received(self, session_id: str, request_id: str) -> None:
        """释放请求输入的压缩保护，失败也保留输入，不新增任何助手事件。"""
        if self._session_delivery is None:
            return
        compression = await self._session_delivery.finish_received(session_id, request_id)
        if compression is not None:
            self._compression_queue.submit(compression)

    @asynccontextmanager
    async def _received_input(
        self, session_id: str, prompt: str, *, request_id: str,
        chat_context: ChatContext | None, enabled: bool = True,
    ) -> AsyncIterator[None]:
        """非队列请求在准入之前登记，退出或取消时结束保护，不能遗留永久在途状态。"""
        if not enabled or self._session_delivery is None:
            yield
            return
        try:
            # 登记写入可能在取消发生后才提交，因此接收操作本身也必须受 finally 覆盖。
            await self.receive_queued(
                session_id, prompt, request_id=request_id, chat_context=chat_context,
            )
            yield
        finally:
            await self.finish_received(session_id, request_id)

    @property
    def model_router(self) -> ContentModelRouter:
        """返回当前内容模型路由器。"""

        return self._model_router

    async def ask(
        self,
        session_id: str,
        prompt: str,
        *,
        user: str | None = None,
        user_name: str | None = None,
        chat_context: ChatContext | None = None,
        image_urls: Sequence[str] = (),
        image_material_ids: Sequence[str] = (),
        video_urls: Sequence[str] = (),
    ) -> str:
        """生成回复并在成功后写入普通聊天历史。"""

        return await self._run(
            session_id,
            prompt,
            delivery=None,
            user=user,
            user_name=user_name,
            chat_context=chat_context,
            image_urls=image_urls,
            image_material_ids=image_material_ids,
            video_urls=video_urls,
        )

    async def ask_once(
        self,
        session_id: str,
        prompt: str,
        *,
        system_prompt: str,
        user: str | None = None,
        user_name: str | None = None,
        chat_context: ChatContext | None = None,
        image_urls: Sequence[str] = (),
        image_material_ids: Sequence[str] = (),
        video_urls: Sequence[str] = (),
    ) -> str:
        """专用指令可使用空机器人提示词，不读取或写入普通聊天历史。"""

        normalized_system_prompt = system_prompt.strip()
        return await self._run(
            session_id,
            prompt,
            delivery=None,
            user=user,
            user_name=user_name,
            chat_context=chat_context,
            image_urls=image_urls,
            image_material_ids=image_material_ids,
            video_urls=video_urls,
            system_messages=(
                (ChatMessage(role="system", content=normalized_system_prompt),)
                if normalized_system_prompt else ()
            ),
            persist_history=False,
            use_agent=False,
            use_web_search=False,
        )

    async def respond(
        self,
        session_id: str,
        prompt: str,
        delivery: ReplyDelivery,
        *,
        user: str | None = None,
        user_name: str | None = None,
        chat_context: ChatContext | None = None,
        image_urls: Sequence[str] = (),
        image_material_ids: Sequence[str] = (),
        video_urls: Sequence[str] = (),
        chat_tools: Sequence[ChatToolName] = (),
        tool_session_token: str | None = None,
        recent_group_context: str | None = None,
    ) -> str:
        """生成并投递回复，仅在投递成功后提交对话历史。"""

        return await self._run(
            session_id,
            prompt,
            delivery=delivery,
            user=user,
            user_name=user_name,
            chat_context=chat_context,
            image_urls=image_urls,
            image_material_ids=image_material_ids,
            video_urls=video_urls,
            chat_tools=chat_tools,
            tool_session_token=tool_session_token,
            recent_group_context=recent_group_context,
        )

    def prepare_queued(self, prompt: str) -> str:
        """在请求写入 Redis 前完成输入校验。"""

        return self._prompt_builder.normalize(prompt)

    async def validate_queued(
        self, prompt: str, *, image_urls: Sequence[str] = (), video_urls: Sequence[str] = (),
        image_material_ids: Sequence[str] = (),
        chat_tools: Sequence[ChatToolName] = (),
    ) -> None:
        """自然聊天统一预检 Agent 模型，不消费额度或写入会话历史。"""
        if self._model_guard is None:
            return
        # 无工具时也由 Agent 回答，预设模型必须与实际执行保持一致。
        category = ContentCategory.IMAGE_UNDERSTANDING if image_urls or image_material_ids else (
            ContentCategory.TEXT
        )
        await self._model_guard.ensure_enabled(
            self._model_router.model_for(category), capability="agent",
        )

    def accept_immediate(self, session_id: str, prompt: str) -> str:
        """在请求直接执行前完成输入校验和冷却额度消费。"""

        normalized = self._prompt_builder.normalize(prompt)
        self._request_coordinator.accept(session_id)
        return normalized

    async def generate_queued(
        self,
        session_id: str,
        prompt: str,
        *,
        request_id: str,
        user: str | None = None,
        user_name: str | None = None,
        chat_context: ChatContext | None = None,
        image_urls: Sequence[str] = (),
        image_material_ids: Sequence[str] = (),
        video_urls: Sequence[str] = (),
        chat_tools: Sequence[ChatToolName] = (),
        tool_session_token: str | None = None,
        recent_group_context: str | None = None,
        laya_decision: bool = False,
        system_prompt_append: str | None = None,
    ) -> str:
        """生成排队文本结果，不投递也不提交历史。"""

        system_messages = None
        if system_prompt_append is not None:
            normalized_append = system_prompt_append.strip()
            if not normalized_append:
                raise ValueError("system prompt append cannot be empty")
            system_messages = self._prompt_builder.system_messages_with(
                ChatMessage(role="system", content=normalized_append)
            )
        return await self._run(
            session_id,
            prompt,
            delivery=None,
            user=user,
            user_name=user_name,
            chat_context=chat_context,
            image_urls=image_urls,
            image_material_ids=image_material_ids,
            video_urls=video_urls,
            chat_tools=chat_tools,
            tool_session_token=tool_session_token,
            recent_group_context=recent_group_context,
            commit_history=False,
            reject_busy=False,
            consume_cooldown=False,
            request_id=request_id,
            laya_decision=laya_decision,
            system_messages=system_messages,
            record_user_input=False,
        )

    async def generate_failure_explanation_queued(
        self,
        session_id: str,
        prompt: str,
        *,
        request_id: str,
        error_type: str,
        user: str | None = None,
        user_name: str | None = None,
        chat_context: ChatContext | None = None,
    ) -> str:
        """通过无工具 chatOne 把原请求和脱敏异常类型转换为用户说明。"""

        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", error_type):
            raise ValueError("error type is invalid")
        failure_prompt = (
            f"{prompt}\n\n"
            f"[系统状态：本地图片工作流执行失败；异常类型：{error_type}。"
            "请只向用户解释失败并给出可操作的重试建议，不要声称图片已生成。]"
        )
        return await self._run(
            session_id,
            failure_prompt,
            delivery=None,
            user=user,
            user_name=user_name,
            chat_context=chat_context,
            image_urls=(),
            video_urls=(),
            chat_tools=(),
            tool_session_token=None,
            recent_group_context=None,
            commit_history=False,
            reject_busy=False,
            consume_cooldown=False,
            request_id=f"{request_id[:120]}-error",
            use_agent=False,
            use_web_search=False,
            record_user_input=False,
        )

    async def generate_reply_queued(
        self,
        session_id: str,
        prompt: str,
        *,
        request_id: str,
        instruction: str,
        untrusted_facts: str | None = None,
        user: str | None = None,
        user_name: str | None = None,
        chat_context: ChatContext | None = None,
        use_agent: bool = False,
    ) -> str:
        """由 Composer 独立限流生成无历史回复；完成通知可显式交给 Agent 表达。"""

        system_messages = self._prompt_builder.system_messages_with(
            ChatMessage(role="system", content=instruction)
        )
        return await self._run(
            session_id,
            prompt,
            delivery=None,
            user=user,
            user_name=user_name,
            chat_context=chat_context,
            image_urls=(),
            video_urls=(),
            system_messages=system_messages,
            untrusted_facts=untrusted_facts,
            persist_history=False,
            use_agent=use_agent,
            use_web_search=False,
            chat_tools=(),
            tool_session_token=None,
            recent_group_context=None,
            commit_history=False,
            reject_busy=False,
            consume_cooldown=False,
            request_id=request_id,
            reply_composition=True,
        )

    async def generate_image_start_queued(
        self,
        session_id: str,
        prompt: str,
        *,
        request_id: str,
        user: str | None = None,
        user_name: str | None = None,
        chat_context: ChatContext | None = None,
    ) -> str:
        """在独立有界入口生成开画答复，避免重入父 Agent 持有的会话锁。"""

        async with self._image_start_semaphore:
            return await self.generate_reply_queued(
                session_id,
                prompt,
                request_id=f"{request_id[:120]}-start",
                instruction=(
                    "你准备开始生成图片。请根据当前人设向用户回复一句简短的开画提示，"
                    "表达即将开始画、请稍候；任务会在答复送达后提交。"
                    "不要声称任务已提交或图片已经完成，不要调用工具。"
                ),
                user=user,
                user_name=user_name,
                chat_context=chat_context,
            )

    async def commit_queued(
        self,
        session_id: str,
        prompt: str,
        delivered_answer: str,
        *,
        chat_context: ChatContext | None = None,
        request_id: str | None = None,
    ) -> None:
        """在全部 OneBot 消息片段投递成功后提交排队会话。"""

        normalized = self._prompt_builder.normalize(prompt)
        history_prompt = self._prompt_builder.history_user_prompt(
            normalized,
            chat_context,
        )
        normalized_answer = delivered_answer.strip()
        if not normalized_answer:
            raise ValueError("delivered answer cannot be empty")
        commit_options = {} if request_id is None else {"request_id": request_id}
        compression = await finish_database_write(
            lambda: self._store.commit_exchange(
                session_id,
                history_prompt,
                normalized_answer,
                **commit_options,
            ),
        )
        if compression is not None:
            self._compression_queue.submit(compression)

    async def respond_media(
        self,
        session_id: str,
        prompt: str,
        delivery: MediaReplyDelivery,
        *,
        output_type: MediaOutputType,
        timeout_seconds: float,
        max_bytes: int,
        image_format: str | None,
        voice: str | None,
        audio_format: str | None,
        user: str,
        user_name: str | None = None,
        chat_context: ChatContext | None = None,
        recent_group_context: str | None = None,
    ) -> str:
        """先登记原用户输入，生成媒体并在投递成功后确认回复。"""

        request_id = f"qq-{uuid.uuid4().hex}"
        async with self._received_input(
            session_id, prompt, request_id=request_id, chat_context=chat_context,
        ):
            response = await self._media_generator.generate(
                session_id,
                prompt,
                output_type=output_type,
                timeout_seconds=timeout_seconds,
                max_bytes=max_bytes,
                image_format=image_format,
                voice=voice,
                audio_format=audio_format,
                user=user,
                user_name=user_name,
                chat_context=chat_context,
                recent_group_context=recent_group_context,
                reject_busy=True,
                consume_cooldown=True,
                request_id=request_id,
            )
            delivered_answer = await delivery(response)
            if not delivered_answer:
                raise ValueError("media reply delivery must return non-empty delivered content")
            await self.commit_queued(
                session_id,
                prompt,
                delivered_answer,
                chat_context=chat_context,
                request_id=request_id if self._session_delivery is not None else None,
            )
            return delivered_answer

    async def generate_media_queued(
        self,
        session_id: str,
        prompt: str,
        *,
        request_id: str,
        output_type: MediaOutputType,
        timeout_seconds: float,
        max_bytes: int,
        image_format: str | None,
        voice: str | None,
        audio_format: str | None,
        user: str,
        user_name: str | None = None,
        chat_context: ChatContext | None = None,
        recent_group_context: str | None = None,
    ) -> ChatMediaResponse:
        """生成排队媒体结果，不投递也不提交历史。"""

        return await self._media_generator.generate(
            session_id,
            prompt,
            output_type=output_type,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            image_format=image_format,
            voice=voice,
            audio_format=audio_format,
            user=user,
            user_name=user_name,
            chat_context=chat_context,
            recent_group_context=recent_group_context,
            reject_busy=False,
            consume_cooldown=False,
            request_id=request_id,
        )

    async def _run(
        self,
        session_id: str,
        prompt: str,
        *,
        delivery: ReplyDelivery | None,
        user: str | None,
        user_name: str | None,
        chat_context: ChatContext | None,
        image_urls: Sequence[str],
        image_material_ids: Sequence[str] = (),
        video_urls: Sequence[str],
        system_messages: Sequence[ChatMessage] | None = None,
        untrusted_facts: str | None = None,
        persist_history: bool = True,
        use_agent: bool | None = None,
        use_web_search: bool | None = None,
        chat_tools: Sequence[ChatToolName] = (),
        tool_session_token: str | None = None,
        recent_group_context: str | None = None,
        commit_history: bool = True,
        reject_busy: bool = True,
        consume_cooldown: bool = True,
        request_id: str | None = None,
        reply_composition: bool = False,
        laya_decision: bool = False,
        record_user_input: bool = True,
    ) -> str:
        """自然聊天默认进入 Agent；专用指令及内部正文生成显式关闭工具能力。"""

        normalized = self._prompt_builder.normalize(prompt)
        current_prompt = (
            self._prompt_builder.history_user_prompt(normalized, chat_context)
            if chat_context is not None
            else self._prompt_builder.current_user_prompt(normalized, user_name)
        )
        if bool(chat_tools or laya_decision) != bool(tool_session_token):
            raise ValueError("chat tools and tool session token must be provided together")
        effective_request_id = request_id or f"qq-{uuid.uuid4().hex}"

        route = self._route_selector.select(
            normalized,
            available_tools=chat_tools,
            web_search_enabled=self._web_search_enabled,
        )
        effective_agent = True if use_agent is None else use_agent
        effective_web = route.web_search if use_web_search is None else use_web_search

        admission = (
            nullcontext()
            if reply_composition
            else self._request_coordinator.request(
                session_id,
                reject_busy=reject_busy,
                consume_cooldown=consume_cooldown,
            )
        )
        async with (
            self._received_input(
                session_id, normalized, request_id=effective_request_id, chat_context=chat_context,
                enabled=persist_history and record_user_input,
            ),
            admission,
        ):
            conversation_context = (
                await self.conversation_context(session_id, exclude_request_id=effective_request_id)
                if persist_history
                else None
            )
            messages = self._prompt_builder.messages(
                current_prompt,
                conversation_context=conversation_context,
                system_messages=system_messages,
                persist_history=persist_history,
                recent_group_context=recent_group_context,
            )
            if untrusted_facts is not None:
                messages.insert(
                    -1,
                    ChatMessage(
                        role="user",
                        content=(
                            "[UNTRUSTED_REPLY_FACTS_JSON]\n"
                            f"{untrusted_facts}\n"
                            "[/UNTRUSTED_REPLY_FACTS_JSON]"
                        ),
                    ),
                )
            category = (
                ContentCategory.IMAGE_UNDERSTANDING
                if image_urls or image_material_ids
                else ContentCategory.TEXT
            )
            model_id = self._model_router.model_for(category)
            if self._model_guard is not None:
                from agent_bot.ai.routing import AiRouteRequest, select_route

                route_request = AiRouteRequest(
                    agent=effective_agent,
                    web=effective_agent and effective_web,
                    tools=bool(chat_tools),
                    laya=laya_decision,
                    chat_tools=tuple(chat_tools),
                    images=bool(image_urls or image_material_ids),
                    videos=bool(video_urls),
                )
                route_method = getattr(self.client, "route", None)
                decision = (
                    route_method(route_request) if route_method else select_route(route_request)
                )
                # Agent 自身解析模型一致性快照，普通文本才单独预检模型目录。
                if decision.capability not in {"agent", "media"}:
                    await self._model_guard.ensure_enabled(model_id, capability=decision.capability)
            context_options = {} if chat_context is None else {"chat_context": chat_context}
            if image_material_ids:
                context_options["image_material_ids"] = tuple(image_material_ids)
            if reply_composition:
                # 完成通知只表达已确定事实，新增共享知识工具不能扩大原有通知能力。
                context_options["allow_knowledge"] = False
            if laya_decision:
                context_options["laya_decision"] = True
            if chat_tools or laya_decision:
                answer = await self.client.complete(
                    model=model_id,
                    session_id=session_id,
                    prompt=current_prompt,
                    messages=messages,
                    request_id=effective_request_id,
                    user=user,
                    image_urls=tuple(image_urls),
                    video_urls=tuple(video_urls),
                    use_agent=effective_agent,
                    use_web_search=(not laya_decision and effective_agent and effective_web),
                    chat_tools=tuple(chat_tools),
                    tool_session_token=tool_session_token,
                    **context_options,
                )
            else:
                answer = await self.client.complete(
                    model=model_id,
                    session_id=session_id,
                    prompt=current_prompt,
                    messages=messages,
                    request_id=effective_request_id,
                    user=user,
                    image_urls=tuple(image_urls),
                    video_urls=tuple(video_urls),
                    use_agent=effective_agent,
                    use_web_search=effective_agent and effective_web,
                    **context_options,
                )
            delivered_answer = await delivery(answer) if delivery is not None else answer
            if not delivered_answer and not has_tool_images():
                raise ValueError("reply delivery must return non-empty delivered content")
            if persist_history and commit_history and delivered_answer:
                await self.commit_queued(
                    session_id,
                    normalized,
                    delivered_answer,
                    chat_context=chat_context,
                    request_id=effective_request_id,
                )
            return delivered_answer

    async def aclose(self) -> None:
        """停止本服务持有的后台记忆压缩；客户端由运行时统一关闭。"""

        await self._compression_queue.aclose()

    async def reset(self, session_id: str) -> bool:
        """在没有同会话进行中请求时清除冷却状态和历史。"""

        async def clear_store() -> bool:
            """清空事务完成后才允许释放会话锁。"""

            return await finish_database_write(lambda: self._store.clear(session_id))

        return await self._request_coordinator.reset(session_id, clear_store)
