"""聊天工具会话的权限计算与上下文绑定。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from nonebot import get_bots
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent

from agent_bot.ai.catalog import AiCatalog
from agent_bot.config import RuntimeConfig
from agent_bot.integrations.gpt_sovits import GptSoVitsClient
from agent_bot.integrations.memory_vectors import MemoryVectorIndex
from agent_bot.integrations.onebot.memory_source import (
    direct_memory_source,
    has_user_memory_source,
    queued_source_event_id,
)
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.memory.contracts import QuotedMemorySource
from agent_bot.memory.embeddings import EmbeddingClient
from agent_bot.memory.retrieval import HybridMemorySearch
from agent_bot.models import ChatContext, ChatToolName
from agent_bot.services.chat_reply import ChatReplyCollector
from agent_bot.services.chat_tools import ChatToolContext, ImageTaskService, SpeechCollector
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.services.naraka import NarakaQueryService
from agent_bot.services.request_deadline import remaining_timeout
from agent_bot.services.tagged_memory import MemoryIdentity, MemorySession
from agent_bot.services.tool_operations import MentionEffect, ToolOperationStore
from agent_bot.services.tool_sessions import ToolSessionStore
from agent_bot.services.workflow_execution import (
    WorkflowExecutionService,
    WorkflowImageCollector,
)
from agent_bot.services.workflow_inputs import workflow_input_scope

GroupMemberRefresher = Callable[[Bot, int], Awaitable[object]]


@dataclass(frozen=True, slots=True)
class ChatToolSessionLease:
    """Agent 工具令牌及其请求级媒体收集器。"""

    tools: Sequence[ChatToolName]
    token: str | None
    collector: WorkflowImageCollector | None = None
    speech_collector: SpeechCollector | None = None
    reply_collector: ChatReplyCollector | None = None

    def __iter__(self) -> Iterator[object]:
        """保持原有 `(tools, token)` 解构调用兼容。"""

        yield self.tools
        yield self.token


class ChatToolSessionManager:
    """按消息场景创建最小权限的 MCP 工具会话。"""

    def __init__(
        self,
        *,
        config_provider: Callable[[], RuntimeConfig],
        store_provider: Callable[[], ToolSessionStore | None],
        naraka_service_provider: Callable[[], NarakaQueryService | None],
        group_member_refresher: GroupMemberRefresher,
        chat_context_provider: Callable[[MessageEvent], ChatContext] | None = None,
        workflow_service_provider: Callable[[], WorkflowExecutionService | None] = (
            lambda: None
        ),
        tts_service_provider: Callable[[], GptSoVitsClient | None] = lambda: None,
        operation_store_provider: Callable[[], ToolOperationStore | None] = lambda: None,
        memory_store_provider: Callable[[], TaggedMemoryStore | None] = lambda: None,
        image_tasks_provider: Callable[[], ImageTaskService | None] = lambda: None,
        material_store_provider: Callable[[], object | None] = lambda: None,
        catalog_provider: Callable[[], AiCatalog | None] = lambda: None,
    ) -> None:
        """保存热更新配置和工具依赖提供器。"""

        self._config_provider = config_provider
        self._store_provider = store_provider
        self._naraka_service_provider = naraka_service_provider
        self._group_member_refresher = group_member_refresher
        self._chat_context_provider = chat_context_provider
        self._workflow_service_provider = workflow_service_provider
        self._tts_service_provider = tts_service_provider
        self._operation_store_provider = operation_store_provider
        self._memory_store_provider = memory_store_provider
        self._image_tasks_provider = image_tasks_provider
        self._material_store_provider = material_store_provider
        self._catalog_provider = catalog_provider

    def available_tools(self, event: MessageEvent) -> tuple[ChatToolName, ...]:
        """计算当前事件可用的工具白名单。"""

        return self._available_tools(group=isinstance(event, GroupMessageEvent))

    def available_group_tools(self) -> tuple[ChatToolName, ...]:
        """计算不依赖入站用户事件的普通群聊工具白名单。"""

        return self._available_tools(group=True)

    def _available_tools(self, *, group: bool) -> tuple[ChatToolName, ...]:
        """按运行开关与真实服务授权，直接生图仅依赖持久队列而不依赖 Comfy。"""

        config = self._config_provider()
        if not config.chat_mcp_enabled or self._store_provider() is None:
            return ()
        tools: list[ChatToolName] = []
        if config.chat_job_enabled:
            tools.append("job")
        if config.chat_tagged_memory_enabled and self._memory_store_provider() is not None:
            tools.append("memory")
        if self._naraka_service_provider() is not None:
            tools.extend(("naraka_query", "naraka_search"))
        if group and config.chat_mention_tool_enabled:
            tools.append("mention_member")
        if config.chat_image_generation_enabled and self._image_tasks_provider() is not None:
            tools.append("image_generate")
        if (
            config.chat_local_workflow_enabled
            and self._workflow_service_provider() is not None
        ):
            tools.extend(
                (
                    "local_workflow_list",
                    "local_workflow_generate_image",
                    "local_workflow_generate_video",
                )
            )
        if self._image_tasks_provider() is not None:
            tools.extend(("local_workflow_tasks", "local_workflow_cancel"))
        if config.chat_tts_enabled and self._tts_service_provider() is not None:
            tools.append("tts_generate")
        return tuple(tools)

    @asynccontextmanager
    async def open_chat_session(
        self,
        bot: Bot,
        event: MessageEvent,
        session_id: str,
        *,
        image_urls: tuple[str, ...] = (),
    ) -> AsyncIterator[ChatToolSessionLease]:
        """为同步请求绑定工具身份，所有绘图工具共享请求级额度。"""

        tools = self.available_tools(event)
        config = self._config_provider()
        store = self._store_provider()
        if not tools or store is None:
            yield ChatToolSessionLease(tools=(), token=None)
            return

        workflow_enabled = bool(
            {"image_generate", "local_workflow_generate_image", "local_workflow_generate_video"}
            & set(tools)
        )
        collector = WorkflowImageCollector() if workflow_enabled else None
        speech_collector = SpeechCollector() if "tts_generate" in tools else None

        async def group_member_provider() -> object:
            """按需刷新当前群成员目录。"""

            if not isinstance(event, GroupMessageEvent):
                return []
            return await self._group_member_refresher(bot, event.group_id)

        chat_context = (
            self._chat_context_provider(event) if self._chat_context_provider is not None else None
        )
        context = ChatToolContext(
            session_id=session_id,
            user_id=str(event.user_id),
            user_name=chat_context.sender_name if chat_context is not None else None,
            chat_context=chat_context,
            group_id=event.group_id if isinstance(event, GroupMessageEvent) else None,
            bot_user_id=bot.self_id,
            bot=bot,
            naraka_service=self._naraka_service_provider(),
            group_member_provider=(
                group_member_provider if isinstance(event, GroupMessageEvent) else None
            ),
            allowed_tools=frozenset(tools),
            workflow_service=self._workflow_service_provider(),
            workflow_collector=collector,
            image_tasks=self._image_tasks_provider(),
            image_urls=image_urls,
            tts_service=self._tts_service_provider(),
            tts_collector=speech_collector,
            deadline_at=time.monotonic() + remaining_timeout(config.chat_timeout_seconds),
        )
        context.memory_session = self._memory_session(
            context, source_text=event.original_message.extract_plain_text(),
            source_event_id=f"qq:{bot.self_id}:{event.message_id}",
            source_time=datetime.fromtimestamp(event.time, timezone.utc),
        )
        with direct_memory_source(
            session_id=session_id, bot_id=bot.self_id, user_id=str(event.user_id),
            message_id=event.message_id, created_at=event.time,
            secret=config.chat_session_secret.get_secret_value(),
            group_id=str(event.group_id) if isinstance(event, GroupMessageEvent) else None,
            source_text=event.original_message.extract_plain_text(),
        ):
            async with store.open(context) as token:
                yield ChatToolSessionLease(
                    tools=tools,
                    token=token,
                    collector=collector,
                    speech_collector=speech_collector,
                    reply_collector=context.reply_collector,
                )

    def queued_bot(self, bot_id: str) -> Bot:
        """获取队列任务指定的已连接 OneBot 实例。"""

        bot = get_bots().get(bot_id)
        if not isinstance(bot, Bot):
            raise ConnectionError("OneBot is not connected")
        return bot

    @asynccontextmanager
    async def open_queued_session(
        self,
        job: ConversationJob,
    ) -> AsyncIterator[ChatToolSessionLease]:
        """恢复固化白名单及绘图回执，避免重启后再次生成已提交的图片。"""

        if not job.chat_tools and not job.laya_decision:
            yield ChatToolSessionLease(tools=(), token=None)
            return
        config = self._config_provider()
        store = self._store_provider()
        if store is None or not config.chat_mcp_enabled:
            raise RuntimeError("queued chat tools are not available")
        if job.laya_decision:
            raise RuntimeError("queued Laya decision is not available")

        available_tools = set(self._available_tools(group=job.scene == "group"))
        workflow_service = self._workflow_service_provider()
        tts_service = self._tts_service_provider()
        if not set(job.chat_tools).issubset(available_tools):
            raise RuntimeError("queued chat tool configuration changed")

        bot = self.queued_bot(job.bot_id)
        group_id = int(job.group_id) if job.group_id is not None else None
        workflow_enabled = bool(
            {"image_generate", "local_workflow_generate_image", "local_workflow_generate_video"}
            & set(job.chat_tools)
        )
        collector = WorkflowImageCollector() if workflow_enabled else None
        speech_collector = (
            SpeechCollector() if "tts_generate" in job.chat_tools else None
        )

        async def group_member_provider() -> object:
            """按需刷新队列任务对应群成员目录。"""

            if group_id is None:
                return []
            return await self._group_member_refresher(bot, group_id)

        context = ChatToolContext(
            session_id=job.session_id,
            user_id=job.user_id,
            user_name=job.user_name,
            chat_context=job.chat_context,
            group_id=group_id,
            bot_user_id=job.bot_id,
            bot=bot,
            naraka_service=self._naraka_service_provider(),
            group_member_provider=group_member_provider if group_id is not None else None,
            allowed_tools=frozenset(job.chat_tools),
            workflow_service=workflow_service,
            workflow_collector=collector,
            image_tasks=self._image_tasks_provider(),
            image_urls=job.image_urls,
            image_material_ids=job.image_material_ids,
            material_store=self._material_store_provider(),
            workflow_request_scope=workflow_input_scope(job.job_id),
            tts_service=tts_service,
            tts_collector=speech_collector,
            job_id=job.job_id,
            operation_store=self._operation_store_provider(),
            deadline_at=time.monotonic() + remaining_timeout(config.chat_timeout_seconds),
        )
        # 只有真人 QQ 入队请求可将持久化原提示作为来源；自动发言和任务产物无本人原话。
        has_source = has_user_memory_source(job)
        context.memory_session = self._memory_session(
            context, source_text=job.memory_source_text if has_source else None,
            source_event_id=queued_source_event_id(job),
            source_time=(datetime.fromtimestamp(job.created_at, timezone.utc)
                         if has_source else None),
            request_id=job.request_id,
            quoted_source=job.memory_quoted_source if has_source else None,
        )
        if context.operation_store is not None:
            if "mention_member" in job.chat_tools:
                saved = await context.operation_store.completed(job.job_id, "send_reply")
                if saved is not None:
                    await context.reply_collector.select(
                        saved, job_id=job.job_id, store=context.operation_store,
                    )
                # 升级前 mention_member 已经发送的任务不得在恢复时再次发送。
                previous = await context.operation_store.completed(job.job_id, "mention_member")
                context.reply_collector.already_delivered = isinstance(previous, MentionEffect)
            for tool in (
                "image_generate",
                "local_workflow_generate_image",
                "local_workflow_generate_video",
                "tts_generate",
            ):
                if tool in job.chat_tools:
                    saved = await context.operation_store.completed(job.job_id, tool)
                    if saved is not None:
                        await context.restore_effect(saved)
        async with store.open(context) as token:
            yield ChatToolSessionLease(
                tools=job.chat_tools,
                token=token,
                collector=collector,
                speech_collector=speech_collector,
                reply_collector=context.reply_collector,
            )

    def _memory_session(
        self, context: ChatToolContext, *, source_text: str | None = None,
        source_event_id: str | None = None, source_time: datetime | None = None,
        request_id: str | None = None,
        quoted_source: QuotedMemorySource | None = None,
    ) -> MemorySession | None:
        """按真人事件绑定单请求权限，队列引用单列为资料来源，不替换本人原话。"""
        store = self._memory_store_provider()
        if "memory" not in context.allowed_tools or store is None:
            return None
        if not context.bot_user_id:
            raise ValueError("memory requires a bound bot")
        config = self._config_provider()
        catalog = self._catalog_provider() if config.chat_memory_embedding_model else None
        semantic_search = (
            HybridMemorySearch(MemoryVectorIndex(store), EmbeddingClient(
                catalog, config.chat_memory_embedding_model,
            )) if catalog is not None else None
        )
        return MemorySession(
            store=store,
            identity=MemoryIdentity(
                bot_id=context.bot_user_id, user_id=context.user_id,
                session_id=context.session_id,
                group_id=str(context.group_id) if context.group_id is not None else None,
                source_text=source_text, source_event_id=source_event_id, source_time=source_time,
                request_id=request_id,
                quoted_source=quoted_source,
            ),
            deadline_at=context.deadline_at,
            semantic_search=semantic_search,
        )
