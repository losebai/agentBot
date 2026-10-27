"""NapCatQQ / OneBot V11 聊天插件。"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from nonebot import (
    get_bots,
    get_driver,
    get_plugin_config,
    logger,
)
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.matcher import Matcher, current_bot, current_event
from nonebot.plugin import PluginMetadata
from nonebot.typing import T_State

from agent_bot.bootstrap import (
    database_params,
    load_bootstrap_config,
    load_queue_bootstrap_config,
    load_redis_bootstrap_config,
)
from agent_bot.config import PluginConfig, RuntimeConfig
from agent_bot.database_pool import create_database_pool
from agent_bot.group_members import (
    GroupMemberStore,
)
from agent_bot.integrations.mcp import ChatMcpServer
from agent_bot.integrations.onebot import (
    AutoSpeakBot,
    ChatToolSessionManager,
    ConversationHandler,
    ConversationQueueProcessor,
    DrawingCommandHandler,
    EventContextService,
    GroupAutoSpeakTrigger,
    MessageRouting,
    NarakaCommandHandler,
    SummaryCommandHandler,
)
from agent_bot.integrations.onebot.ai_jobs import AiJobPoller, AiJobRunner
from agent_bot.integrations.onebot.outgoing import DeliveryScope, OutgoingRecorder
from agent_bot.integrations.onebot.replies import OneBotReplyGateway, OneBotReplyService
from agent_bot.integrations.redis_queue import build_conversation_queue
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.integrations.tool_operations import RedisToolOperationStore
from agent_bot.integrations.workflow_store import WorkflowStore
from agent_bot.materials.onebot import install_material_receivers
from agent_bot.materials.receiver import MaterialReceiver
from agent_bot.media.config import MediaConfig
from agent_bot.media.resources import MediaResources
from agent_bot.memory_store import (
    ChatMemoryStoreError,
    GroupContextPolicy,
)
from agent_bot.models import ChatContext, ChatMediaResponse, ChatToolName
from agent_bot.plugin.handlers import (
    PluginHandlerDependencies,
    register_plugin_handlers,
)
from agent_bot.plugin.lifecycle import (
    RuntimeLeaseGate,
    apply_runtime_settings,
    settings_refresh_loop,
    start_background_task,
    stop_background_task,
)
from agent_bot.plugin.matchers import build_plugin_matchers
from agent_bot.plugin.runtime import (
    RuntimeState as _RuntimeState,
)
from agent_bot.plugin.runtime import (
    build_runtime_state,
    close_runtime_state,
    effective_runtime_config,
)
from agent_bot.plugins.qqbot.binding import current_context
from agent_bot.runtime_reload import SettingsReloader
from agent_bot.services.chat import ChatService
from agent_bot.services.chat_tools import build_chat_tool_registry
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.services.naraka_formatting import (
    format_naraka_match_detail,
    format_naraka_player_search,
    format_naraka_summary,
)
from agent_bot.services.replies import LlmReplyComposer
from agent_bot.services.tool_operations import ToolOperationStore
from agent_bot.services.tool_sessions import ToolSessionStore
from agent_bot.services.workflows import WorkflowCatalogImporter
from agent_bot.session_context import SessionContextPolicy
from agent_bot.session_delivery import SessionDeliveryStore
from agent_bot.session_schema import SessionContextSchema
from agent_bot.settings import SettingsStore, VersionedSettings

_MEMORY_CLEANUP_INTERVAL_SECONDS = 3600
_MEMORY_CLEANUP_BATCH_SIZE = 500

__plugin_meta__ = PluginMetadata(
    name="chat",
    description="通过 NapCatQQ 和 NoneBot2 使用本地 AI 服务的多轮聊天插件",
    usage=(
        "私聊直接发送消息，群聊 @机器人或包含配置名称开始聊天；超级用户可用 /new 重置当前对话；"
        "引用消息后发送 /总结，可总结普通消息、合并聊天记录和图片；"
        "/画图 <提示词> 使用默认本地工作流直接生成图片；"
        "/laya 通用决策指令尚未实现；"
        "/at <QQ号或@成员> <内容> 在群聊中提醒成员；"
        "启用语音后由 AI 根据对话选择是否发送原生 QQ 语音；"
        "/永劫 <角色ID、昵称或比赛ID> 查询战绩；"
        "/永劫搜索 <昵称> 查找角色列表；"
        "群聊自然对话需要 @机器人或包含配置名称；"
        "群聊关键词可自动 @发送者并固定回复"
    ),
    config=PluginConfig,
    supported_adapters={"~onebot.v11"},
)


def _get_auto_speak_bot(bot_id: str) -> AutoSpeakBot | None:
    """按机器人 ID 返回已连接的自动发言适配器。"""

    bot = get_bots().get(bot_id)
    return bot if isinstance(bot, Bot) else None


def _log_auto_speak_error(error_type: str) -> None:
    """记录已脱敏的自动发言异常类型。"""

    logger.warning("自动发言本轮失败：{}", error_type)


_context = current_context()
_database_params = (_context.runtime.database_params if _context is not None
                    else database_params(load_bootstrap_config()))


def _legacy_startup(action):
    """只有独立 NoneBot 兼容入口注册回调，统一宿主直接调用生命周期。"""
    return get_driver().on_startup(action) if _context is None else action


def _legacy_shutdown(action):
    """宿主插件通过管理器关闭，不注册第二个关闭所有者。"""
    return get_driver().on_shutdown(action) if _context is None else action

_queue_bootstrap = load_queue_bootstrap_config()
_redis_bootstrap = load_redis_bootstrap_config() if _queue_bootstrap.backend == "redis" else None
plugin_config = get_plugin_config(RuntimeConfig)
_database_pool = (_context.runtime.database_pool if _context is not None else create_database_pool(
    _database_params,
    max_size=plugin_config.chat_db_pool_size,
    acquire_timeout_seconds=plugin_config.chat_db_timeout_seconds,
    connect_timeout_seconds=plugin_config.chat_db_timeout_seconds,
    read_timeout_seconds=plugin_config.chat_db_timeout_seconds,
    write_timeout_seconds=plugin_config.chat_db_timeout_seconds,
))
_settings_store = (_context.runtime.settings_store if _context is not None else SettingsStore(
    _database_params, connect=_database_pool.connect, static_config=plugin_config,
))
# 媒体资源按进程持有；构造不联网、不创建目录，运行时热刷新复用同一个实例。
_media_resources = (_context.runtime.media_resources if _context is not None else
                    MediaResources(MediaConfig.from_environment(), _database_params,
                                   connect=_database_pool.connect))
_material_receiver = MaterialReceiver(_media_resources.materials)
_session_delivery_store = SessionDeliveryStore(
    lambda: conversation_store, receipts_provider=lambda: _tool_operations(),
    live_receipts_provider=lambda: _outgoing_recorder.confirmed_receipts,
    memory_store_provider=lambda: (
        _tagged_memory_store if plugin_config.chat_tagged_memory_enabled else None
    ),
)
_group_member_store = GroupMemberStore(_database_params, connect=_database_pool.connect)
_workflow_store = (_context.runtime.workflow_store if _context is not None else
                   WorkflowStore(_database_params, connect=_database_pool.connect))
_memory_schema = SessionContextSchema(_database_params, connect=_database_pool.connect)
_tagged_memory_store = TaggedMemoryStore(
    _database_params, connect=_database_pool.connect,
    secret=plugin_config.chat_session_secret.get_secret_value(),
)


def _build_runtime_state(
    config: RuntimeConfig,
    previous: _RuntimeState | None = None,
    *, core_state=None,
) -> _RuntimeState:
    """使用入口依赖构建可原子替换的运行时快照。"""

    from agent_bot.agent.tools.chat import ChatToolBridge

    return build_runtime_state(
        config,
        database=_database_params,
        database_pool=_database_pool,
        bot_provider=_get_auto_speak_bot,
        auto_speak_error_handler=_log_auto_speak_error,
        auto_speak_job_submitter=lambda job: conversation_queue.enqueue(job),
        auto_speak_tools_provider=lambda: _tool_session_manager.available_group_tools(),
        chat_service_factory=ChatService,
        previous=previous,
        chat_tool_bridge=ChatToolBridge(lambda: _chat_mcp_server),
        media_resources=_media_resources,
        core_state=(core_state if core_state is not None else
                    _context.runtime.state if _context is not None else None),
        session_delivery=_session_delivery_store,
    )


def _publish_runtime_state(state: _RuntimeState) -> None:
    """发布运行时快照，同时更新适配器名称，避免旧名称继续触发 to_me。"""

    global plugin_config, _session_secret, _naraka_default_role_id
    global access_policy, ai_client, model_guard, conversation_store, chat_service
    global group_recent_context_store
    global message_counter, auto_speak_service, naraka_client, naraka_service
    global comfy_client, workflow_service, tts_client

    state.conversation_store.update_policy(
        SessionContextPolicy(
            history_turns=state.config.chat_history_turns,
            max_history_chars=state.config.chat_history_max_chars,
            memory_enabled=state.config.chat_memory_enabled,
            max_memory_chars=state.config.chat_memory_max_chars,
        )
    )
    state.group_recent_context_store.update_policy(
        GroupContextPolicy(
            message_limit=state.config.chat_group_recent_context_messages,
            max_chars=state.config.chat_group_recent_context_max_chars,
            ttl_seconds=state.config.chat_group_recent_context_ttl_seconds,
        )
    )
    plugin_config = state.config
    # OneBot 在插件规则之前识别名称，适配器与插件必须使用同一份名称快照。
    get_driver().config.nickname = set(state.config.nickname)
    _session_secret = state.session_secret
    _naraka_default_role_id = state.naraka_default_role_id
    access_policy = state.access_policy
    ai_client = state.ai_client
    model_guard = state.model_guard
    conversation_store = state.conversation_store
    group_recent_context_store = state.group_recent_context_store
    chat_service = state.chat_service
    message_counter = state.message_counter
    auto_speak_service = state.auto_speak_service
    naraka_client = state.naraka_client
    naraka_service = state.naraka_service
    comfy_client = state.comfy_client
    workflow_service = state.workflow_service
    tts_client = state.tts_client


async def _outgoing_event_scope(request_id: str | None) -> DeliveryScope:
    """Matcher 发送从真实事件解析会话，队列发送由处理器显式绑定原任务身份。"""
    import hashlib

    bot, event = current_bot.get(), current_event.get()
    if not isinstance(bot, Bot) or not isinstance(event, MessageEvent):
        raise RuntimeError("OUTGOING_CONTEXT_UNAVAILABLE")
    session = _event_context.session_id(bot, event)
    generation = await chat_service.history_generation(session)
    identity = hashlib.sha256(f"{bot.self_id}:{event.message_id}".encode()).hexdigest()
    return DeliveryScope(
        session, generation, request_id or f"qq-event-{identity}",
        bot_id=str(bot.self_id),
        group_id=str(event.group_id) if isinstance(event, GroupMessageEvent) else None,
    )


_outgoing_recorder = OutgoingRecorder(
    service_provider=lambda: chat_service, store_provider=lambda: _session_delivery_store,
    materials_provider=lambda: _media_resources.materials, scope_provider=_outgoing_event_scope,
    receipts_provider=lambda: _tool_operations(),
    group_message_recorder=lambda scope, delivery_id, text: _event_context.record_sent_group_text(
        bot_id=scope.bot_id, group_id=scope.group_id, delivery_id=delivery_id, content=text,
    ),
)
_reply_gateway = OneBotReplyGateway(lambda: plugin_config, recorder=_outgoing_recorder)
_runtime_state = _build_runtime_state(plugin_config) if _context is None else None
if _runtime_state is not None:
    _publish_runtime_state(_runtime_state)
_event_context = EventContextService(
    config_provider=lambda: plugin_config,
    access_policy_provider=lambda: access_policy,
    session_secret_provider=lambda: _session_secret,
    group_member_store_provider=lambda: _group_member_store,
    recent_context_store_provider=lambda: group_recent_context_store,
)
_group_auto_speak_trigger = GroupAutoSpeakTrigger(
    service_provider=lambda: auto_speak_service,
    recent_context_store_provider=lambda: group_recent_context_store,
    record_recent_group_text=lambda bot, event: (
        _event_context.record_recent_group_text(bot, event)
    ),
    chat_context_provider=lambda bot, event: _chat_context(bot, event),
)
_message_routing = MessageRouting(
    config_provider=lambda: plugin_config,
    recent_group_text_provider=lambda event: _event_context.recent_group_text(event),
)

_tool_registry = build_chat_tool_registry()
_tool_session_store: ToolSessionStore | None = None
_chat_mcp_server: ChatMcpServer | None = None
if plugin_config.chat_mcp_enabled:
    _tool_session_store = ToolSessionStore(
        ttl_seconds=plugin_config.chat_mcp_session_ttl_seconds,
        max_sessions=plugin_config.chat_mcp_max_sessions,
    )
    _chat_mcp_server = ChatMcpServer(
        store=_tool_session_store,
        registry=_tool_registry,
        allowed_hosts=plugin_config.chat_mcp_allowed_hosts,
    )
    get_driver().server_app.mount(
        plugin_config.chat_mcp_mount_path,
        _chat_mcp_server.app,
        name="chat-mcp",
    )

def _tool_operations() -> ToolOperationStore:
    """工具副作用与任务使用同一持久后端，热刷新时读取最新保留期限。"""
    if _queue_bootstrap.backend == "sqlite":
        from agent_bot.integrations.sqlite_tool_operations import SqliteToolOperationStore

        return SqliteToolOperationStore(
            _queue_bootstrap.sqlite_path, key_prefix="chat",
            ttl_seconds=plugin_config.chat_queue_result_ttl_seconds,
        )
    assert _redis_bootstrap is not None and _redis_client is not None
    return RedisToolOperationStore(
        _redis_client, key_prefix=_redis_bootstrap.key_prefix,
        ttl_seconds=plugin_config.chat_queue_result_ttl_seconds,
    )
if _context is not None:
    from agent_bot.agent.tools.chat import ChatToolBridge

    _context.runtime.bind_chat_bridge(ChatToolBridge(lambda: _chat_mcp_server))



def _plugin_tool_names() -> tuple[ChatToolName, ...]:
    """只有宿主已启动的 B站服务提供工具；停用时不沿用配置列表授予能力。"""
    if _context is None or _context.runtime.bilibili_service is None:
        return ()
    from agent_bot.plugins.bilibili.contracts import BILIBILI_TOOLS

    return tuple(sorted(BILIBILI_TOOLS))


_tool_session_manager = ChatToolSessionManager(
    config_provider=lambda: plugin_config,
    store_provider=lambda: _tool_session_store,
    naraka_service_provider=lambda: naraka_service,
    group_member_refresher=lambda bot, group_id: _refresh_group_members(bot, group_id),
    chat_context_provider=lambda event: _event_context.chat_context(event),
    workflow_service_provider=lambda: workflow_service,
    image_tasks_provider=lambda: image_queue.image_tasks,
    tts_service_provider=lambda: tts_client,
    memory_store_provider=lambda: (
        _tagged_memory_store if plugin_config.chat_tagged_memory_enabled else None
    ),
    material_store_provider=lambda: _media_resources.materials,
    operation_store_provider=_tool_operations,
    catalog_provider=lambda: model_guard.catalog,
    plugin_tools_provider=_plugin_tool_names,
)

_runtime_gate = _context.runtime.gate if _context is not None else RuntimeLeaseGate()
_channel_ready = asyncio.Event()
_channel_closed = False
_auto_speak_task: asyncio.Task[None] | None = None
_ai_job_task: asyncio.Task[None] | None = None
_ai_job_runner_task: asyncio.Task[None] | None = None
_settings_refresh_task: asyncio.Task[None] | None = None
_memory_cleanup_task: asyncio.Task[None] | None = None


@asynccontextmanager
async def _open_chat_tool_session(
    bot: Bot,
    event: MessageEvent,
    session_id: str,
):
    """打开当前聊天请求的工具会话。"""

    async with _tool_session_manager.open_chat_session(
        bot,
        event,
        session_id,
    ) as session:
        yield session


def _queued_bot(bot_id: str) -> Bot:
    """获取队列任务指定的 OneBot 实例。"""

    return _tool_session_manager.queued_bot(bot_id)


@asynccontextmanager
async def _open_queued_tool_session(job: ConversationJob):
    """恢复队列任务入队时允许的工具会话。"""

    async with _tool_session_manager.open_queued_session(job) as session:
        yield session


_reply_composer = LlmReplyComposer(
    lambda: chat_service,
    timeout_seconds_provider=lambda: plugin_config.chat_timeout_seconds,
)
_reply_service = OneBotReplyService(_reply_composer, _reply_gateway)


class _ConversationProcessor(ConversationQueueProcessor):
    """把插件热更新依赖接入通用队列处理器。"""

    def __init__(self) -> None:
        """使用延迟提供器保留测试替换和运行时热更新能力。"""

        super().__init__(
            config_provider=lambda: plugin_config,
            chat_service_provider=lambda: chat_service,
            runtime_lease=lambda: _runtime_lease(),
            tool_session_provider=lambda job: _open_queued_tool_session(job),
            bot_provider=lambda bot_id: _queued_bot(bot_id),
            reply_composer=_reply_composer,
            reply_gateway=_reply_gateway,
            operation_store_provider=_tool_operations,
            image_submitter=lambda job: image_queue.enqueue(job),
            workflow_service_provider=lambda: workflow_service,
            media_store_provider=lambda: _media_resources.store,
            outgoing_recorder=_reply_gateway.recorder,
            receive_queued=lambda *args, **kwargs: chat_service.receive_queued(*args, **kwargs),
            finish_received=lambda session_id, request_id: chat_service.finish_received(
                session_id, request_id,
            ),
        )


_queue_resources = None
_redis_client = None
conversation_queue = None
image_queue = None


def initialize_channel_runtime() -> None:
    """公共快照已建立后分配 QQ 客户端和队列，同步准备只注册处理器。"""
    global _runtime_state, _queue_resources, _redis_client
    global conversation_queue, image_queue, _conversation_queue_store

    if _runtime_state is None:
        _runtime_state = _build_runtime_state(plugin_config)
        _publish_runtime_state(_runtime_state)
    if _queue_resources is not None:
        return
    if _queue_bootstrap.backend == "sqlite":
        from agent_bot.integrations.sqlite_queue import build_sqlite_conversation_queue

        _queue_resources = build_sqlite_conversation_queue(
            _queue_bootstrap.sqlite_path, "chat", plugin_config, _ConversationProcessor(),
        )
        _redis_client = None
    else:
        assert _redis_bootstrap is not None
        _queue_resources = build_conversation_queue(
            _redis_bootstrap, plugin_config, _ConversationProcessor(),
        )
        _redis_client = _queue_resources.redis_client
    _conversation_queue_store = _queue_resources.store
    conversation_queue = _queue_resources.queue
    image_queue = _queue_resources.image_queue


if _context is None:
    initialize_channel_runtime()



def _effective_runtime_config(
    candidate: RuntimeConfig,
    current: RuntimeConfig,
) -> tuple[RuntimeConfig, frozenset[str]]:
    """保留需要重启的当前配置，并返回允许热更新的部分。"""

    return effective_runtime_config(candidate, current)


def _log_pending_restart_fields(
    revision: int,
    restart_fields: frozenset[str],
) -> None:
    """记录本次热更新中需要重启才能生效的字段。"""

    if restart_fields:
        logger.warning(
            "配置 revision={} 含需重启字段：{}",
            revision,
            ",".join(sorted(restart_fields)),
        )


@asynccontextmanager
async def _runtime_lease():
    """登记一次运行时使用，避免与热更新交叉。"""

    await wait_channel_ready()
    async with _runtime_gate.lease():
        yield


async def wait_channel_ready() -> None:
    """规则检查同样等待启动，防止正向连接早到事件访问未发布的权限和状态。"""
    if _context is not None:
        await _channel_ready.wait()
        if _channel_closed:
            raise RuntimeError("QQBot 已关闭")


def mark_channel_ready() -> None:
    """队列和后台生产者全部启动成功后才放行启动期间到达的平台事件。"""
    _channel_ready.set()


@_legacy_startup
async def initialize_chat_memory() -> None:
    """会话结构始终只校验；已有部署须显式迁移，普通启动不写数据库结构。"""

    await asyncio.to_thread(_memory_schema.validate)
    if _context is not None:
        await asyncio.to_thread(_tagged_memory_store.validate)
    else:
        await asyncio.to_thread(_tagged_memory_store.initialize)


async def _memory_cleanup_loop() -> None:
    """仅清理独立的临时群背景，会话上下文没有过期或自动删除机制。"""

    while True:
        await asyncio.sleep(_MEMORY_CLEANUP_INTERVAL_SECONDS)
        try:
            group_deleted = await asyncio.to_thread(
                group_recent_context_store.cleanup_expired,
                limit=_MEMORY_CLEANUP_BATCH_SIZE,
            )
            if group_deleted:
                logger.info(
                    "过期群背景清理完成：groups={}",
                    group_deleted,
                )
        except asyncio.CancelledError:
            raise
        except ChatMemoryStoreError as exc:
            logger.warning("过期聊天记忆清理失败：{}", type(exc).__name__)


@_legacy_startup
async def start_memory_cleanup() -> None:
    """幂等启动持久记忆过期清理任务。"""

    global _memory_cleanup_task
    _memory_cleanup_task = start_background_task(
        _memory_cleanup_task,
        _memory_cleanup_loop,
        name="chat-memory-cleanup",
    )


async def stop_memory_cleanup() -> None:
    """停止持久记忆过期清理任务。"""

    global _memory_cleanup_task
    task = _memory_cleanup_task
    await stop_background_task(
        task,
        error_message="持久记忆清理任务异常结束：{}",
    )
    if _memory_cleanup_task is task:
        _memory_cleanup_task = None


@_legacy_startup
async def initialize_workflow_catalog() -> None:
    """先增补旧表兼容列，再读取工作流或从配置目录导入全部 JSON。"""

    if not plugin_config.chat_local_workflow_enabled:
        return
    await asyncio.to_thread(_workflow_store.prepare_for_startup)
    directory = plugin_config.chat_local_workflow_directory
    if directory is None:
        await asyncio.to_thread(_workflow_store.list_all, limit=1)
        return
    definitions = await asyncio.to_thread(WorkflowCatalogImporter().scan, Path(directory))
    await asyncio.to_thread(_workflow_store.upsert, definitions)


@_legacy_startup
async def start_conversation_queue() -> None:
    """启动明确选择的本地或 Redis 持久队列，不在连接失败时切换后端。"""

    await image_queue.start()
    await conversation_queue.start()


@_legacy_startup
async def start_chat_mcp() -> None:
    """启动已配置的聊天 MCP 服务。"""

    if _chat_mcp_server is not None:
        await _chat_mcp_server.start()


@_legacy_startup
async def start_auto_speak() -> None:
    """幂等启动自动发言后台任务。"""

    global _auto_speak_task
    if auto_speak_service is None or not auto_speak_service.runs_on_interval:
        return
    _auto_speak_task = start_background_task(
        _auto_speak_task,
        auto_speak_service.run,
        name="chat-auto-speak",
    )


@_legacy_startup
async def start_ai_job_polling() -> None:
    """分别启动执行与投递轮询，两者均在租约内读取最新配置。"""
    global _ai_job_task, _ai_job_runner_task
    poller = AiJobPoller(
        config_provider=lambda: plugin_config,
        client_provider=lambda: chat_service.client,
        queue_provider=lambda: conversation_queue,
        bot_ids_provider=lambda: [key for key, bot in get_bots().items() if isinstance(bot, Bot)],
        runtime_lease=_runtime_lease,
    )
    _ai_job_task = start_background_task(_ai_job_task, poller.run, name="chat-ai-jobs")
    if _context is not None:
        return
    runner = AiJobRunner(
        config_provider=lambda: plugin_config,
        client_provider=lambda: chat_service.client,
        runtime_lease=_runtime_lease,
    )
    _ai_job_runner_task = start_background_task(
        _ai_job_runner_task, runner.run, name="chat-python-jobs"
    )


async def stop_ai_job_polling() -> None:
    """先取消执行并记录中断，再停止投递生产者；消费者由外层关闭。"""
    global _ai_job_task, _ai_job_runner_task
    runner = _ai_job_runner_task
    await stop_background_task(runner, error_message="Python 任务执行器异常结束：{}")
    if _ai_job_runner_task is runner:
        _ai_job_runner_task = None
    task = _ai_job_task
    await stop_background_task(task, error_message="AI 任务结果调度器异常结束：{}")
    if _ai_job_task is task:
        _ai_job_task = None


async def stop_auto_speak() -> None:
    """取消自动发言，并等待其清理完成。"""

    global _auto_speak_task
    task = _auto_speak_task
    await stop_background_task(
        task,
        error_message="自动发言任务异常结束：{}",
    )
    if _auto_speak_task is task:
        _auto_speak_task = None


async def _close_runtime_state(state: _RuntimeState) -> None:
    """关闭已退出服务的 HTTP 客户端。"""

    await close_runtime_state(state)


async def _apply_runtime_settings(stored: VersionedSettings) -> None:
    """等待在途消息完成后，原子发布一个经过完整校验的新运行时快照。"""

    if _context is not None:
        await _context.runtime._apply_settings(stored)
        return
    await apply_runtime_settings(
        stored,
        current_state=_runtime_state,
        gate=_runtime_gate,
        effective_config_resolver=lambda candidate, current: _effective_runtime_config(
            candidate,
            current,
        ),
        state_builder=lambda config, previous: _build_runtime_state(config, previous),
        stop_auto_speak=lambda: stop_auto_speak(),
        start_auto_speak=lambda: start_auto_speak(),
        state_publisher=_activate_runtime_state,
        state_closer=lambda state: _close_runtime_state(state),
        pending_fields_logger=_log_pending_restart_fields,
    )


def _activate_runtime_state(state: _RuntimeState) -> None:
    """发布运行时快照并更新入口持有的当前状态。"""

    global _runtime_state
    _runtime_state = state
    _publish_runtime_state(state)


_initial_settings_revision = getattr(
    get_driver().config,
    "chat_settings_revision",
    1,
)
if type(_initial_settings_revision) is not int or _initial_settings_revision < 1:
    _initial_settings_revision = 1
_settings_reloader = None if _context is not None else SettingsReloader(
    _settings_store,
    initial_revision=_initial_settings_revision,
    apply=_apply_runtime_settings,
)


def get_runtime_state() -> _RuntimeState:
    """供 agentBot 管理层读取当前运行时；使用客户端前必须获取租约。"""
    if _runtime_state is None:
        raise RuntimeError("QQBot 尚未启动")
    return _runtime_state


def get_applied_settings_revision() -> int:
    """返回已完成热更新处理的版本；启动字段的差异由管理层另外展示。"""
    return _context.runtime.revision if _context is not None else _settings_reloader.revision


def get_settings_store() -> SettingsStore:
    """复用插件持有的配置存储和数据库池，避免重复装配。"""
    return _settings_store


def acquire_runtime():
    """供管理对话测试与机器人共享热更新租约。"""
    return _runtime_lease()


async def check_management_readiness() -> bool:
    """检查所选队列存储与消费者；本地模式不访问 Redis，QQ 未连接不影响就绪。"""
    try:
        storage = _queue_resources if _queue_bootstrap.backend == "sqlite" else _redis_client
        reachable = await asyncio.wait_for(storage.ping(), timeout=2)
    except Exception:
        return False
    return bool(reachable and conversation_queue.running and image_queue.running)


async def _settings_refresh_loop() -> None:
    """定期检查数据库设置 revision 并应用变更。"""

    await settings_refresh_loop(lambda: plugin_config, _settings_reloader)


@_legacy_startup
async def start_settings_refresh() -> None:
    """启动数据库配置 revision 轮询任务。"""

    global _settings_refresh_task
    _settings_refresh_task = start_background_task(
        _settings_refresh_task,
        _settings_refresh_loop,
        name="chat-settings-refresh",
    )


async def stop_settings_refresh() -> None:
    """停止配置轮询并等待任务清理完成。"""

    global _settings_refresh_task
    task = _settings_refresh_task
    await stop_background_task(
        task,
        error_message="配置自动刷新任务异常结束：{}",
    )
    if _settings_refresh_task is task:
        _settings_refresh_task = None


async def _is_authorized(event: MessageEvent) -> bool:
    """使用当前访问策略校验消息来源。"""

    await wait_channel_ready()
    return await _event_context.is_authorized(event)


_material_matchers = install_material_receivers(
    receiver_provider=lambda: _material_receiver, config_provider=lambda: plugin_config,
    policy_provider=lambda: access_policy, secret_provider=lambda: _session_secret,
    is_authorized=_is_authorized,
    session_id_provider=lambda bot, event: _event_context.session_id(bot, event),
    runtime_lease=_runtime_lease, wait_ready=wait_channel_ready,
)


async def _resolve_group_name(bot: Bot, group_id: int) -> str | None:
    """读取并规范化当前群名称。"""

    return await _event_context.resolve_group_name(bot, group_id)


async def _chat_context(bot: Bot, event: MessageEvent) -> ChatContext:
    """构建当前消息的强类型聊天上下文。"""

    group_name = (
        await _resolve_group_name(bot, event.group_id)
        if isinstance(event, GroupMessageEvent)
        else None
    )
    return _event_context.chat_context(event, group_name=group_name)


async def _refresh_group_members(
    bot: Bot,
    group_id: int,
    *,
    group_name: object = None,
) -> object:
    """刷新单个群的成员目录。"""

    resolved_group_name = (
        group_name
        if isinstance(group_name, str)
        else await _resolve_group_name(bot, group_id)
    )
    return await _event_context.refresh_group_members(
        bot,
        group_id,
        group_name=resolved_group_name,
    )


async def _load_group_members(bot: Bot, group_id: int) -> object:
    """读取持久化的群成员目录。"""

    return await _event_context.load_group_members(bot, group_id)


async def _sync_bot_group_members(bot: Bot) -> None:
    """同步机器人加入的全部群成员目录。"""

    await _event_context.sync_bot_group_members(
        bot,
        refresher=_refresh_group_members,
    )


async def _handle_group_member_notice_impl(
    bot: Bot,
    event: GroupIncreaseNoticeEvent | GroupDecreaseNoticeEvent,
) -> None:
    """按群成员增减通知更新目录。"""

    await _event_context.handle_group_member_notice(
        bot,
        event,
        refresher=_refresh_group_members,
    )


_KEYWORD_REPLY_STATE_KEY = "chat_keyword_at_reply"


async def _matches_keyword_at_reply(event: MessageEvent, state: T_State) -> bool:
    """匹配群聊关键词并把固定回复写入 matcher 状态。"""

    await wait_channel_ready()
    return await _message_routing.matches_keyword_reply(
        event,
        state,
        state_key=_KEYWORD_REPLY_STATE_KEY,
    )


async def _targets_natural_chat(event: MessageEvent) -> bool:
    """判断消息是否明确指向当前机器人。"""

    await wait_channel_ready()
    return await _message_routing.targets_natural_chat(event)


async def _accepts_natural_chat(event: MessageEvent) -> bool:
    """避免已删除的 `/chat` 入口被自然聊天兜底成普通 AI 请求。"""

    await wait_channel_ready()
    return await _message_routing.accepts_natural_chat(event)


async def _accepts_recent_group_context(event: MessageEvent) -> bool:
    """筛选仅用于记录近期上下文的普通群聊文本。"""

    await wait_channel_ready()
    return await _message_routing.accepts_recent_group_context(
        event,
        target_checker=_targets_natural_chat,
    )


_matchers = build_plugin_matchers(
    plugin_config,
    is_authorized=_is_authorized,
    matches_keyword_reply=_matches_keyword_at_reply,
    targets_natural_chat=_targets_natural_chat,
    accepts_natural_chat=_accepts_natural_chat,
    accepts_recent_group_context=_accepts_recent_group_context,
)
group_member_lifecycle = _matchers.group_member_lifecycle
group_member_notice = _matchers.group_member_notice
reset_command = _matchers.reset_command
draw_command = _matchers.draw_command
laya_command = _matchers.laya_command
summary_command = _matchers.summary_command
at_command = _matchers.at_command
naraka_command = _matchers.naraka_command
naraka_search_command = _matchers.naraka_search_command
keyword_at_reply = _matchers.keyword_at_reply
natural_chat = _matchers.natural_chat
group_recent_context = _matchers.group_recent_context


_conversation_handler = ConversationHandler(
    material_receiver_provider=lambda: _material_receiver,
    reply_service=_reply_service,
    config_provider=lambda: plugin_config,
    chat_service_provider=lambda: chat_service,
    message_counter_provider=lambda: message_counter,
    queue_provider=lambda: conversation_queue,
    session_id_provider=lambda bot, event: _event_context.session_id(bot, event),
    llm_user_name_provider=lambda event: _event_context.llm_user_name(event),
    current_user_name_provider=lambda event: _event_context.current_user_name(event),
    chat_context_provider=lambda bot, event: _chat_context(bot, event),
    recent_context_provider=lambda bot, event: (
        _event_context.recent_group_context_for_request(bot, event)
    ),
    available_tools_provider=lambda event: _tool_session_manager.available_tools(event),
    group_members_loader=lambda bot, group_id: _load_group_members(bot, group_id),
    group_members_refresher=lambda bot, group_id: _refresh_group_members(bot, group_id),
)


async def _deliver_media_answer(
    matcher: type[Matcher],
    response: ChatMediaResponse,
    *,
    reply_prefix: MessageSegment | None = None,
) -> str:
    """发送媒体回答并返回会话历史占位。"""

    return await _conversation_handler.deliver_media_answer(
        matcher,
        response,
        reply_prefix=reply_prefix,
    )


_summary_handler = SummaryCommandHandler(
    material_registrar_factory=_conversation_handler.material_registrar,
    config_provider=lambda: plugin_config,
    chat_service_provider=lambda: chat_service,
    message_counter_provider=lambda: message_counter,
    session_id_provider=lambda bot, event: _event_context.session_id(bot, event),
    llm_user_name_provider=lambda event: _event_context.llm_user_name(event),
    current_user_name_provider=lambda event: _event_context.current_user_name(event),
    reply_service=_reply_service,
    reply_intent_factory=_conversation_handler.reply_intent,
)
_drawing_handler = DrawingCommandHandler(
    config_provider=lambda: plugin_config,
    workflow_service_provider=lambda: workflow_service,
    chat_service_provider=lambda: chat_service,
    queue_provider=lambda: conversation_queue,
    image_queue_provider=lambda: image_queue,
    message_counter_provider=lambda: message_counter,
    session_id_provider=lambda bot, event: _event_context.session_id(bot, event),
    llm_user_name_provider=lambda event: _event_context.llm_user_name(event),
    current_user_name_provider=lambda event: _event_context.current_user_name(event),
    chat_context_provider=lambda bot, event: _chat_context(bot, event),
    reply_service=_reply_service,
    reply_intent_factory=_conversation_handler.reply_intent,
)
_naraka_handler = NarakaCommandHandler(
    service_provider=lambda: naraka_service,
    default_role_id_provider=lambda: _naraka_default_role_id,
    session_id_provider=lambda bot, event: _event_context.session_id(bot, event),
    reply_service=_reply_service,
    reply_intent_factory=_conversation_handler.reply_intent,
    summary_formatter=lambda result: format_naraka_summary(result),
    match_formatter=lambda result: format_naraka_match_detail(result),
    player_search_formatter=lambda result: format_naraka_player_search(result),
)


async def _handle_conversational_mention(
    bot: Bot,
    event: GroupMessageEvent,
) -> Message | None:
    """委托自然语言群成员提醒解析。"""

    return await _conversation_handler.conversational_mention(
        bot,
        event,
        matcher=natural_chat,
    )


_registered_handlers = register_plugin_handlers(
    _matchers,
    PluginHandlerDependencies(
        reply_service=_reply_service,
        reply_intent_factory=_conversation_handler.reply_intent,
        runtime_lease=lambda: _runtime_lease(),
        is_authorized=lambda event: _is_authorized(event),
        is_superuser=lambda event: _event_context.is_superuser(event),
        sync_group_members=lambda bot: _sync_bot_group_members(bot),
        handle_member_notice=lambda bot, event: _handle_group_member_notice_impl(
            bot, event
        ),
        handle_reset=lambda bot, event: _conversation_handler.reset(
            bot, event, matcher=reset_command
        ),
        handle_draw=lambda bot, event, argument: _drawing_handler.handle(
            bot,
            event,
            argument,
            matcher=draw_command,
        ),
        handle_laya=lambda bot, event, argument: _conversation_handler.laya_command(
            bot, event, argument, matcher=laya_command,
        ),
        handle_mention=lambda bot, event, argument: (
            _conversation_handler.mention_command(bot, event, argument, matcher=at_command)
        ),
        handle_natural_chat=lambda bot, event: _conversation_handler.natural_chat(
            bot,
            event,
            matcher=natural_chat,
            mention_handler=_handle_conversational_mention,
        ),
        record_recent_group_text=lambda bot, event: (
            _event_context.record_recent_group_text(bot, event)
        ),
        handle_group_auto_speak=lambda bot, event: (
            _group_auto_speak_trigger.handle(bot, event)
        ),
        handle_summary=lambda bot, event: _summary_handler.handle(
            bot, event, matcher=summary_command
        ),
        handle_naraka=lambda bot, event, argument: _naraka_handler.handle_query(
            bot, event, argument, matcher=naraka_command
        ),
        handle_naraka_search=lambda bot, event, argument: (
            _naraka_handler.handle_search(
                bot,
                event,
                argument,
                matcher=naraka_search_command,
            )
        ),
        keyword_state_key=_KEYWORD_REPLY_STATE_KEY,
    ),
)
handle_group_member_lifecycle = _registered_handlers.handle_group_member_lifecycle
handle_group_member_notice = _registered_handlers.handle_group_member_notice
handle_reset = _registered_handlers.handle_reset
handle_draw_command = _registered_handlers.handle_draw_command
handle_laya_command = _registered_handlers.handle_laya_command
handle_at_command = _registered_handlers.handle_at_command
handle_natural_chat = _registered_handlers.handle_natural_chat
handle_group_recent_context = _registered_handlers.handle_group_recent_context
handle_keyword_at_reply = _registered_handlers.handle_keyword_at_reply
handle_summary_command = _registered_handlers.handle_summary_command
handle_naraka_command = _registered_handlers.handle_naraka_command
handle_naraka_search_command = _registered_handlers.handle_naraka_search_command


@_legacy_shutdown
async def close_clients() -> None:
    """请求与客户端退出后关闭媒体清理和文件锁，数据库池最后关闭。"""

    if _context is not None:
        from agent_bot.core.application import close_actions

        global _channel_closed
        _channel_closed = True
        # 部分启动失败也要唤醒早到的事件，让它们明确失败并释放适配器的任务。
        _channel_ready.set()
        storage = _queue_resources if _queue_bootstrap.backend == "sqlite" else _redis_client
        await close_actions([
            stop_auto_speak, stop_ai_job_polling, stop_memory_cleanup,
            *([conversation_queue.stop] if conversation_queue is not None else []),
            *([image_queue.stop] if image_queue is not None else []),
            *([_chat_mcp_server.stop] if _chat_mcp_server is not None else []),
            *([storage.aclose] if storage is not None else []),
            *([lambda: _close_runtime_state(_runtime_state)]
              if _runtime_state is not None else []),
        ])
        return
    await stop_ai_job_polling()
    try:
        try:
            await conversation_queue.stop()
        finally:
            await image_queue.stop()
    finally:
        try:
            storage = _queue_resources if _queue_bootstrap.backend == "sqlite" else _redis_client
            await storage.aclose()
        finally:
            try:
                if _chat_mcp_server is not None:
                    await _chat_mcp_server.stop()
            finally:
                try:
                    await stop_memory_cleanup()
                finally:
                    try:
                        await stop_settings_refresh()
                    finally:
                        try:
                            await stop_auto_speak()
                        finally:
                            try:
                                await _close_runtime_state(_runtime_state)
                            finally:
                                try:
                                    await _media_resources.aclose()
                                finally:
                                    await _database_pool.aclose()


@_legacy_startup
async def start_media_resources() -> None:
    """可选媒体资源启动失败只影响媒体就绪，不阻断纯文本入口。"""
    await _media_resources.start()
