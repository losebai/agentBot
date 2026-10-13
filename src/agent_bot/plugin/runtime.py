"""NoneBot 插件运行时快照的构建与释放。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials
from agent_bot.agent.records import AgentRunStore
from agent_bot.agent.service import AgentService
from agent_bot.agent.tools.chat import ChatToolBridge
from agent_bot.agent.tools.media import AdminMediaTools
from agent_bot.agent.tools.runtime import ToolRuntime
from agent_bot.ai.auto_speak import AutoSpeakDecisionService
from agent_bot.ai.auto_speak_records import AutoSpeakRecords
from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.providers.agent_chat import AgentChatProvider
from agent_bot.ai.providers.agent_ollama import AgentOllamaProvider
from agent_bot.ai.providers.agent_responses import AgentResponsesProvider
from agent_bot.ai.providers.chat_completions import ChatCompletionsProvider
from agent_bot.ai.providers.ollama import OllamaProvider
from agent_bot.ai.providers.responses import ResponsesProvider
from agent_bot.ai.providers.transport import ProviderTransport
from agent_bot.ai.routing import RoutedModelGuard, RoutingAiClient
from agent_bot.ai.service import AiService
from agent_bot.ai.sessions import AiSessionStore
from agent_bot.config import RuntimeConfig
from agent_bot.database_pool import MySqlConnectionPool
from agent_bot.integrations.comfy import ComfyClient
from agent_bot.integrations.gpt_sovits import GptSoVitsClient
from agent_bot.integrations.naraka import NarakaClient
from agent_bot.integrations.onebot.auto_speak import AutoSpeakBot, AutoSpeakService
from agent_bot.integrations.workflow_store import WorkflowStore
from agent_bot.jobs.service import JobService
from agent_bot.jobs.store import JobStore
from agent_bot.jobs.tools import JobTools
from agent_bot.media.download import QQImageDownloader
from agent_bot.media.providers import MediaProvider
from agent_bot.media.resources import MediaResources
from agent_bot.media.service import MediaGenerationService
from agent_bot.memory_store import (
    ConversationMemoryPolicy,
    GroupContextPolicy,
    MySqlConversationMemoryStore,
    MySqlRecentGroupContextStore,
)
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import ChatToolName, LlmRequestOptions
from agent_bot.runtime_fields import RESTART_REQUIRED_FIELDS
from agent_bot.services.auto_speak_decision import AutoSpeakContextBuilder
from agent_bot.services.chat import ChatService
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.services.naraka import NarakaQueryService
from agent_bot.services.request_deadline import remaining_timeout
from agent_bot.services.workflow_execution import WorkflowExecutionService
from agent_bot.settings import (
    DatabaseParams,
    runtime_config_to_plain_values,
)
from agent_bot.state import AccessPolicy, MessageCounter

ChatServiceFactory = Callable[..., ChatService]


@dataclass(frozen=True, slots=True, repr=False)
class RuntimeState:
    """一次完整且可原子替换的插件运行时快照。"""

    config: RuntimeConfig
    session_secret: str
    naraka_default_role_id: str | None
    access_policy: AccessPolicy
    ai_client: RoutingAiClient
    model_guard: RoutedModelGuard
    conversation_store: MySqlConversationMemoryStore
    group_recent_context_store: MySqlRecentGroupContextStore
    chat_service: ChatService
    message_counter: MessageCounter
    auto_speak_service: AutoSpeakService | None
    naraka_client: NarakaClient | None
    naraka_service: NarakaQueryService | None
    comfy_client: ComfyClient | None
    workflow_service: WorkflowExecutionService | None
    tts_client: GptSoVitsClient | None


def build_runtime_state(
    config: RuntimeConfig,
    *,
    database: DatabaseParams,
    database_pool: MySqlConnectionPool,
    bot_provider: Callable[[str], AutoSpeakBot | None],
    auto_speak_error_handler: Callable[[str], None],
    auto_speak_job_submitter: Callable[[ConversationJob], Awaitable[str]],
    auto_speak_tools_provider: Callable[[], Sequence[ChatToolName]],
    chat_service_factory: ChatServiceFactory = ChatService,
    previous: RuntimeState | None = None,
    chat_tool_bridge: ChatToolBridge | None = None,
    media_resources: MediaResources | None = None,
) -> RuntimeState:
    """从类型化配置构建运行时，并复用契约未变化的历史存储。"""

    session_secret = config.chat_session_secret.get_secret_value()
    naraka_default_role_id = (
        config.chat_naraka_default_role_id.get_secret_value()
        if config.chat_naraka_default_role_id is not None
        else None
    )
    access_policy = AccessPolicy(
        allowed_users=config.chat_allowed_users,
        allowed_groups=config.chat_allowed_groups,
        blocked_groups=config.chat_blocked_groups,
        blocked_users=config.chat_blocked_users,
        allow_all=config.chat_allow_all,
    )
    model_router = ContentModelRouter(
        text_model=config.chat_model,
        vision_model=config.chat_vision_model,
        image_model=config.chat_image_model,
        audio_model=config.chat_tts_model,
    )
    catalog = AiCatalog(database, connect=database_pool.connect)
    agent_catalog = AgentCatalog(database, connect=database_pool.connect)
    mcp_connector = McpConnector(McpCredentials())
    job_store = JobStore(database, connect=database_pool.connect)
    python_service = AiService(
        catalog,
        AiSessionStore(database, connect=database_pool.connect),
        CredentialResolver(),
        {
            "openai-responses": ResponsesProvider(ProviderTransport()),
            "openai-chat": ChatCompletionsProvider(ProviderTransport()),
            "ollama": OllamaProvider(ProviderTransport()),
        },
    )
    comfy_client: ComfyClient | None = None
    workflow_service: WorkflowExecutionService | None = None
    if config.chat_local_workflow_enabled:
        comfy_client = ComfyClient(
            base_url=config.chat_local_workflow_base_url,
            timeout_seconds=config.chat_local_workflow_timeout_seconds,
            max_image_bytes=config.chat_image_max_bytes,
        )
        workflow_service = WorkflowExecutionService(
            store=WorkflowStore(database, connect=database_pool.connect),
            client=comfy_client,
        )

    tts_client: GptSoVitsClient | None = None
    if (
        config.chat_tts_enabled
        and config.chat_tts_url is not None
        and config.chat_tts_reference_audio_path is not None
        and config.chat_tts_reference_text is not None
    ):
        tts_client = GptSoVitsClient(
            url=config.chat_tts_url,
            reference_audio_path=config.chat_tts_reference_audio_path,
            reference_text=config.chat_tts_reference_text,
            reference_language=config.chat_tts_reference_language,
            text_language=config.chat_tts_text_language,
            output_directory=config.chat_tts_output_directory,
            speed_factor=config.chat_tts_speed,
            timeout_seconds=config.chat_tts_timeout_seconds,
            max_input_chars=config.chat_tts_max_input_chars,
            max_audio_bytes=config.chat_tts_max_audio_bytes,
            max_concurrent_requests=config.chat_queue_media_workers,
        )

    ai_client = RoutingAiClient(
        python_service,
        timeout_seconds=config.chat_timeout_seconds,
        default_options=LlmRequestOptions(**{
            name: getattr(config, f"chat_llm_{name}")
            for name in LlmRequestOptions.model_fields
        }),
        mcp_services=config.chat_mcp_services,
        media_service=MediaGenerationService(
            catalog, CredentialResolver(), MediaProvider(), media_resources
        )
        if media_resources is not None
        else None,
        agent_code=config.chat_python_agent_code,
        agent_max_steps=config.chat_agent_max_steps,
        web_search_max_steps=config.chat_web_search_max_steps,
        agent_service=AgentService(
            agent_catalog,
            AgentRunStore(
                database,
                connect=database_pool.connect,
                media_store=media_resources.store if media_resources else None,
            ),
            CredentialResolver(),
            {
                "openai-chat": AgentChatProvider(ProviderTransport()),
                "openai-responses": AgentResponsesProvider(ProviderTransport()),
                "ollama": AgentOllamaProvider(ProviderTransport()),
            },
            ToolRuntime(
                mcp_connector,
                chat_tool_bridge,
                job_tools=JobTools(job_store) if config.chat_job_enabled else None,
                admin_media_tools=AdminMediaTools(
                    config,
                    workflow_service,
                    tts_client,
                    media_resources.store,
                    comfy_client=comfy_client,
                    resources=media_resources,
                )
                if media_resources is not None
                else None,
            ),
            search_code=config.chat_python_search_mcp_code,
            media_store=media_resources.store if media_resources else None,
            image_downloader=QQImageDownloader(remaining=lambda: remaining_timeout(15)),
            maximum_images=config.chat_max_images_per_message,
        ),
        job_service=JobService(job_store, python_service),
    )
    model_guard = RoutedModelGuard(
        catalog, agent_catalog=ai_client.agent_service.catalog,
        agent_code=config.chat_python_agent_code,
    )
    conversation_policy = ConversationMemoryPolicy(
        history_turns=config.chat_history_turns,
        max_history_chars=config.chat_history_max_chars,
        ttl_seconds=config.chat_history_ttl_seconds,
        memory_enabled=config.chat_memory_enabled,
        max_memory_chars=config.chat_memory_max_chars,
    )
    conversation_store = (
        previous.conversation_store
        if previous is not None
        else MySqlConversationMemoryStore(
            database,
            policy=conversation_policy,
            connect=database_pool.connect,
        )
    )
    group_context_policy = GroupContextPolicy(
        message_limit=config.chat_group_recent_context_messages,
        max_chars=config.chat_group_recent_context_max_chars,
        ttl_seconds=config.chat_group_recent_context_ttl_seconds,
    )
    group_recent_context_store = (
        previous.group_recent_context_store
        if previous is not None
        else MySqlRecentGroupContextStore(
            database,
            secret=session_secret,
            policy=group_context_policy,
            connect=database_pool.connect,
        )
    )
    chat_service = chat_service_factory(
        client=ai_client,
        store=conversation_store,
        system_prompt=config.chat_system_prompt,
        max_input_chars=config.chat_max_input_chars,
        cooldown_seconds=config.chat_cooldown_seconds,
        max_concurrent_requests=config.chat_max_concurrent_requests,
        max_concurrent_media_requests=config.chat_queue_media_workers,
        max_rate_limit_entries=config.chat_max_sessions,
        model_guard=model_guard,
        model_router=model_router,
        agent_enabled=config.chat_agent_enabled,
        web_search_enabled=config.chat_web_search_enabled,
        mcp_services_enabled=bool(config.chat_mcp_services),
        memory_compression_max_tokens=config.chat_memory_compression_max_tokens,
        memory_compression_queue_capacity=config.chat_memory_compression_queue_capacity,
    )
    message_counter = MessageCounter(
        limit=config.chat_user_message_limit,
        window_seconds=config.chat_user_message_window_seconds,
    )

    auto_speak_service: AutoSpeakService | None = None
    if config.chat_auto_speak_enabled:
        assert config.chat_auto_speak_bot_id is not None
        assert config.chat_auto_speak_mcp_code is not None
        auto_speak_service = AutoSpeakService(
            job_submitter=auto_speak_job_submitter,
            available_tools_provider=auto_speak_tools_provider,
            conversation_context_provider=lambda session_id: asyncio.to_thread(
                conversation_store.context, session_id
            ),
            decision_client=AutoSpeakDecisionService(
                agent_catalog, mcp_connector,
                AutoSpeakRecords(database, connect=database_pool.connect),
                mcp_code=config.chat_auto_speak_mcp_code,
            ),
            context_builder=AutoSpeakContextBuilder(),
            recent_context_store=group_recent_context_store,
            bot_provider=bot_provider,
            bot_id=config.chat_auto_speak_bot_id.get_secret_value(),
            group_ids=config.chat_auto_speak_groups,
            trigger_mode=config.chat_auto_speak_trigger_mode,
            prompt=config.chat_auto_speak_prompt,
            system_prompt=config.chat_system_prompt,
            laya_prompt=config.chat_auto_speak_laya_prompt,
            session_secret=session_secret,
            initial_delay_seconds=config.chat_auto_speak_initial_delay_seconds,
            interval_seconds=config.chat_auto_speak_interval_seconds,
            max_reply_chars=config.chat_auto_speak_max_reply_chars,
            on_error=auto_speak_error_handler,
        )

    naraka_client: NarakaClient | None = None
    naraka_service: NarakaQueryService | None = None
    if (
        config.chat_naraka_cookie is not None
        and config.chat_naraka_heybox_id is not None
    ):
        naraka_client = NarakaClient(
            heybox_id=config.chat_naraka_heybox_id.get_secret_value(),
            cookie=config.chat_naraka_cookie.get_secret_value(),
            server=config.chat_naraka_server,
            season=config.chat_naraka_season,
            battle_tid=config.chat_naraka_battle_tid,
            timeout_seconds=config.chat_naraka_timeout_seconds,
        )
        naraka_service = NarakaQueryService(
            client=naraka_client,
            cooldown_seconds=config.chat_naraka_cooldown_seconds,
            max_concurrent_requests=config.chat_naraka_max_concurrent_requests,
            max_rate_limit_entries=config.chat_max_sessions,
        )

    return RuntimeState(
        config=config,
        session_secret=session_secret,
        naraka_default_role_id=naraka_default_role_id,
        access_policy=access_policy,
        ai_client=ai_client,
        model_guard=model_guard,
        conversation_store=conversation_store,
        group_recent_context_store=group_recent_context_store,
        chat_service=chat_service,
        message_counter=message_counter,
        auto_speak_service=auto_speak_service,
        naraka_client=naraka_client,
        naraka_service=naraka_service,
        comfy_client=comfy_client,
        workflow_service=workflow_service,
        tts_client=tts_client,
    )


def effective_runtime_config(
    candidate: RuntimeConfig,
    current: RuntimeConfig,
) -> tuple[RuntimeConfig, frozenset[str]]:
    """保留需重启字段的当前值，并返回可热更新配置。"""

    restart_fields = frozenset(
        name
        for name in RESTART_REQUIRED_FIELDS
        if getattr(candidate, name) != getattr(current, name)
    )
    if not restart_fields:
        return candidate, restart_fields
    values = runtime_config_to_plain_values(candidate)
    current_values = runtime_config_to_plain_values(current)
    for name in restart_fields:
        values[name] = current_values[name]
    return RuntimeConfig.model_validate(values), restart_fields


async def close_runtime_state(state: RuntimeState) -> None:
    """先停止后台压缩，再关闭已退出运行时持有的外部 HTTP 客户端。"""

    try:
        try:
            await state.chat_service.aclose()
        finally:
            await state.ai_client.aclose()
    finally:
        try:
            if state.naraka_client is not None:
                await state.naraka_client.aclose()
        finally:
            try:
                if state.comfy_client is not None:
                    await state.comfy_client.aclose()
            finally:
                if state.tts_client is not None:
                    await state.tts_client.aclose()
