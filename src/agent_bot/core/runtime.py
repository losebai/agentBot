"""公共 AI、Agent 和媒体快照的构建与释放，不依赖 QQ 生命周期。"""

from __future__ import annotations

from dataclasses import dataclass

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials
from agent_bot.agent.records import AgentRunStore
from agent_bot.agent.service import AgentService
from agent_bot.agent.tools.chat import ChatToolBridge
from agent_bot.agent.tools.media import AdminMediaTools
from agent_bot.agent.tools.runtime import ToolRuntime
from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.providers.langchain import LangChainProvider
from agent_bot.ai.routing import RoutedModelGuard, RoutingAiClient
from agent_bot.ai.service import AiService
from agent_bot.ai.sessions import AiSessionStore
from agent_bot.config import RuntimeConfig
from agent_bot.database_pool import MySqlConnectionPool
from agent_bot.integrations.comfy import ComfyClient
from agent_bot.integrations.gpt_sovits import GptSoVitsClient
from agent_bot.integrations.workflow_store import WorkflowStore
from agent_bot.jobs.service import JobService
from agent_bot.jobs.store import JobStore
from agent_bot.jobs.tools import JobTools
from agent_bot.knowledge.store import KnowledgeStore
from agent_bot.media.download import QQImageDownloader
from agent_bot.media.providers import MediaProvider
from agent_bot.media.resources import MediaResources
from agent_bot.media.service import MediaGenerationService
from agent_bot.models import LlmRequestOptions
from agent_bot.services.request_deadline import remaining_timeout
from agent_bot.services.workflow_execution import WorkflowExecutionService
from agent_bot.settings import (
    DatabaseParams,
)


@dataclass(frozen=True, slots=True, repr=False)
class CoreRuntimeState:
    """公共客户端属于宿主，QQ 快照只借用这些引用。"""

    config: RuntimeConfig
    ai_client: RoutingAiClient
    model_guard: RoutedModelGuard
    agent_catalog: AgentCatalog
    mcp_connector: McpConnector
    comfy_client: ComfyClient | None
    workflow_service: WorkflowExecutionService | None
    tts_client: GptSoVitsClient | None


def build_core_state(
    config: RuntimeConfig,
    *,
    database: DatabaseParams,
    database_pool: MySqlConnectionPool,
    media_resources: MediaResources | None = None,
    workflow_store: WorkflowStore | None = None,
    chat_bridge: ChatToolBridge | None = None,
    knowledge_store: KnowledgeStore | None = None,
) -> CoreRuntimeState:
    """QQ 与管理工具共用公共生图服务，消息队列仍由 QQ 宿主独立装配。"""
    chat_tool_bridge = chat_bridge
    catalog = AiCatalog(database, connect=database_pool.connect)
    agent_catalog = AgentCatalog(database, connect=database_pool.connect)
    mcp_connector = McpConnector(McpCredentials())
    job_store = JobStore(database, connect=database_pool.connect)
    python_service = AiService(
        catalog,
        AiSessionStore(database, connect=database_pool.connect),
        CredentialResolver(),
        {
            "openai-responses": LangChainProvider("openai-responses"),
            "openai-chat": LangChainProvider("openai-chat"),
            "ollama": LangChainProvider("ollama"),
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
            store=workflow_store or WorkflowStore(database, connect=database_pool.connect),
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

    media_service = MediaGenerationService(
        catalog, CredentialResolver(), MediaProvider(), media_resources,
    ) if media_resources is not None else None
    ai_client = RoutingAiClient(
        python_service,
        legacy_system_prompt=config.chat_system_prompt,
        timeout_seconds=config.chat_timeout_seconds,
        default_options=LlmRequestOptions(
            **{name: getattr(config, f"chat_llm_{name}") for name in LlmRequestOptions.model_fields}
        ),
        mcp_services=config.chat_mcp_services,
        media_service=media_service,
        agent_code=config.chat_python_agent_code,
        agent_max_steps=config.chat_agent_max_steps,
        agent_service=AgentService(
            agent_catalog,
            AgentRunStore(
                database,
                connect=database_pool.connect,
                media_store=media_resources.store if media_resources else None,
            ),
            CredentialResolver(),
            {
                "openai-chat": LangChainProvider("openai-chat"),
                "openai-responses": LangChainProvider("openai-responses"),
                "ollama": LangChainProvider("ollama"),
            },
            ToolRuntime(
                mcp_connector,
                chat_tool_bridge,
                knowledge_store=(knowledge_store if knowledge_store is not None else
                                 KnowledgeStore(database, connect=database_pool.connect)),
                job_tools=JobTools(job_store) if config.chat_job_enabled else None,
                material_store=media_resources.materials if media_resources else None,
                media_store=media_resources.store if media_resources else None,
                admin_media_tools=AdminMediaTools(
                    config,
                    workflow_service,
                    tts_client,
                    media_resources.store,
                    comfy_client=comfy_client,
                    resources=media_resources,
                    image_service=media_service,
                )
                if media_resources is not None
                else None,
            ),
            search_code=config.chat_python_search_mcp_code,
            media_store=media_resources.store if media_resources else None,
            material_store=media_resources.materials if media_resources else None,
            image_downloader=QQImageDownloader(remaining=lambda: remaining_timeout(15)),
            maximum_images=config.chat_max_images_per_message,
            maximum_tool_calls=config.chat_agent_max_steps,
        ),
        job_service=JobService(job_store, python_service),
    )
    model_guard = RoutedModelGuard(
        catalog,
        agent_catalog=ai_client.agent_service.catalog,
        agent_code=config.chat_python_agent_code,
    )
    return CoreRuntimeState(
        config,
        ai_client,
        model_guard,
        agent_catalog,
        mcp_connector,
        comfy_client,
        workflow_service,
        tts_client,
    )


async def close_core_state(state: CoreRuntimeState) -> None:
    """关闭各公共客户端，任一失败仍保证后续客户端释放。"""
    try:
        await state.ai_client.aclose()
    finally:
        try:
            if state.comfy_client is not None:
                await state.comfy_client.aclose()
        finally:
            if state.tts_client is not None:
                await state.tts_client.aclose()
