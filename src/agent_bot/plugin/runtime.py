"""NoneBot 插件运行时快照的构建与释放。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from agent_bot.agent.tools.chat import ChatToolBridge
from agent_bot.ai.auto_speak import AutoSpeakDecisionService
from agent_bot.ai.auto_speak_records import AutoSpeakRecords
from agent_bot.ai.llm_auto_speak import LlmAutoSpeakDecisionService
from agent_bot.ai.routing import RoutedModelGuard, RoutingAiClient
from agent_bot.config import RuntimeConfig
from agent_bot.core.runtime import CoreRuntimeState, build_core_state
from agent_bot.database_pool import MySqlConnectionPool
from agent_bot.integrations.comfy import ComfyClient
from agent_bot.integrations.gpt_sovits import GptSoVitsClient
from agent_bot.integrations.naraka import NarakaClient
from agent_bot.integrations.onebot.auto_speak import AutoSpeakBot, AutoSpeakService
from agent_bot.integrations.workflow_store import WorkflowStore
from agent_bot.media.resources import MediaResources
from agent_bot.memory.summary_filter import MemorySummaryFilter
from agent_bot.memory_store import (
    GroupContextPolicy,
    MySqlRecentGroupContextStore,
)
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import ChatToolName
from agent_bot.runtime_fields import RESTART_REQUIRED_FIELDS
from agent_bot.sent_messages import SentMessageStore
from agent_bot.services.auto_speak_decision import AutoSpeakContextBuilder, AutoSpeakDecisionClient
from agent_bot.services.chat import ChatService
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.services.naraka import NarakaQueryService
from agent_bot.services.workflow_execution import WorkflowExecutionService
from agent_bot.session_context import SessionContextPolicy, SessionContextStore
from agent_bot.session_delivery import SessionDeliveryStore
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
    conversation_store: SessionContextStore
    group_recent_context_store: MySqlRecentGroupContextStore
    chat_service: ChatService
    message_counter: MessageCounter
    auto_speak_service: AutoSpeakService | None
    naraka_client: NarakaClient | None
    naraka_service: NarakaQueryService | None
    comfy_client: ComfyClient | None
    workflow_service: WorkflowExecutionService | None
    tts_client: GptSoVitsClient | None
    core_state: CoreRuntimeState
    owns_core: bool


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
    core_state: CoreRuntimeState | None = None,
    sent_messages: SentMessageStore | None = None,
    session_delivery: SessionDeliveryStore | None = None,
) -> RuntimeState:
    """复用统一仓库，并为每次配置快照绑定对应的触发方式和独立间隔。"""

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
    owns_core = core_state is None
    if core_state is None:
        # 直接加载旧 NoneBot 入口时显式构造兼容资源；正式宿主始终提供公共快照。
        from agent_bot.plugins.comfy.plugin import ComfyResources

        comfy_resources = ComfyResources(
            config, WorkflowStore(database, connect=database_pool.connect),
        ) if config.chat_local_workflow_enabled else None
        core_state = build_core_state(
            config, database=database, database_pool=database_pool,
            media_resources=media_resources, chat_bridge=chat_tool_bridge,
            comfy_resources=comfy_resources,
        )
    ai_client, model_guard = core_state.ai_client, core_state.model_guard
    comfy_client, workflow_service = core_state.comfy_client, core_state.workflow_service
    tts_client = core_state.tts_client
    agent_catalog, mcp_connector = core_state.agent_catalog, core_state.mcp_connector
    conversation_policy = SessionContextPolicy(
        history_turns=config.chat_history_turns,
        max_history_chars=config.chat_history_max_chars,
        memory_enabled=config.chat_memory_enabled,
        max_memory_chars=config.chat_memory_max_chars,
    )
    conversation_store = (
        previous.conversation_store
        if previous is not None
        else SessionContextStore(
            database,
            policy=conversation_policy,
            connect=database_pool.connect,
            summary_filter=MemorySummaryFilter(),
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
        web_search_enabled=config.chat_web_search_enabled,
        memory_compression_max_tokens=config.chat_memory_compression_max_tokens,
        memory_compression_queue_capacity=config.chat_memory_compression_queue_capacity,
        sent_messages=sent_messages,
        session_delivery=session_delivery,
        session_secret=session_secret,
    )
    message_counter = MessageCounter(
        limit=config.chat_user_message_limit,
        window_seconds=config.chat_user_message_window_seconds,
    )

    auto_speak_service: AutoSpeakService | None = None
    if config.chat_auto_speak_enabled:
        assert config.chat_auto_speak_bot_id is not None
        decision_client: AutoSpeakDecisionClient
        if config.chat_auto_speak_decision_backend == "llm":
            # 直接普通调用，不能经聊天路由注入 Agent 人设、工具或会话上下文。
            decision_client = LlmAutoSpeakDecisionService(
                ai_client.python_service, model=config.chat_auto_speak_decision_model,
                prompt=config.chat_auto_speak_llm_prompt,
                bot_id=config.chat_auto_speak_bot_id.get_secret_value(),
                bot_name="、".join(sorted(config.nickname)),
            )
        else:
            assert config.chat_auto_speak_mcp_code is not None
            decision_client = AutoSpeakDecisionService(
                agent_catalog, mcp_connector,
                AutoSpeakRecords(database, connect=database_pool.connect),
                mcp_code=config.chat_auto_speak_mcp_code,
            )
        auto_speak_service = AutoSpeakService(
            job_submitter=auto_speak_job_submitter,
            available_tools_provider=auto_speak_tools_provider,
            conversation_context_provider=chat_service.conversation_context,
            decision_client=decision_client,
            context_builder=AutoSpeakContextBuilder(),
            recent_context_store=group_recent_context_store,
            bot_provider=bot_provider,
            bot_id=config.chat_auto_speak_bot_id.get_secret_value(),
            group_ids=config.chat_auto_speak_groups,
            trigger_mode=config.chat_auto_speak_trigger_mode,
            decision_backend=config.chat_auto_speak_decision_backend,
            forward_user_name=config.chat_llm_forward_user_name,
            prompt=config.chat_auto_speak_prompt,
            system_prompt=config.chat_system_prompt,
            system_prompt_provider=ai_client.current_system_prompt,
            group_shared_session=config.chat_group_shared_session,
            laya_prompt=config.chat_auto_speak_laya_prompt,
            session_secret=session_secret,
            initial_delay_seconds=config.chat_auto_speak_initial_delay_seconds,
            interval_seconds=config.chat_auto_speak_interval_seconds,
            message_interval_seconds=config.chat_auto_speak_message_interval_seconds,
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
        core_state=core_state,
        owns_core=owns_core,
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
    """QQ 自持客户端独立释放；兼容入口同时归还自建公共快照的 MCP 缓存。"""
    from agent_bot.core.application import close_actions

    await close_actions([
        state.chat_service.aclose,
        *([state.ai_client.aclose] if state.owns_core else []),
        *([state.core_state.mcp_connector.aclose] if state.owns_core else []),
        *([state.naraka_client.aclose] if state.naraka_client is not None else []),
        *([state.core_state.comfy_resources.aclose]
          if state.owns_core and state.core_state.comfy_resources is not None else []),
        *([state.tts_client.aclose] if state.owns_core and state.tts_client is not None else []),
    ])
