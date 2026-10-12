from __future__ import annotations

import pytest
from nonebot.adapters.onebot.v11 import Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender

from agent_bot.integrations.onebot.tooling import ChatToolSessionManager


def _private_event() -> PrivateMessageEvent:
    """创建不依赖外部 OneBot 的私聊事件。"""

    return PrivateMessageEvent(
        time=1_700_000_000,
        self_id=90001,
        post_type="message",
        sub_type="friend",
        user_id=10001,
        message_type="private",
        message_id=1,
        message=Message("生成语音"),
        original_message=Message("生成语音"),
        raw_message="生成语音",
        font=0,
        sender=Sender(user_id=10001, nickname="测试用户"),
        to_me=True,
    )


@pytest.mark.parametrize("enabled,mcp,configured", [
    (True, True, True), (False, True, True), (True, False, True), (True, True, False),
])
def test_available_tools_includes_tts_only_when_database_client_exists(
    chat_plugin, enabled: bool, mcp: bool, configured: bool,
) -> None:
    """数据库未完成配置时暴露工具，或配置完成后遗漏工具，均应失败。"""

    config = chat_plugin.plugin_config.model_copy(
        update={"dispatch_chat_mcp_enabled": mcp, "dispatch_chat_tts_enabled": enabled}
    )
    client = object() if configured else None
    manager = ChatToolSessionManager(
        config_provider=lambda: config,
        store_provider=lambda: object(),
        naraka_service_provider=lambda: None,
        group_member_refresher=lambda _bot, _group_id: None,
        tts_service_provider=lambda: client,
    )

    expected = ("tts_generate",) if enabled and mcp and configured else ()
    assert manager.available_tools(_private_event()) == expected


def test_available_group_tools_matches_normal_group_chat_capabilities(chat_plugin) -> None:
    """无入站事件的自动发言仍获得普通群聊当前启用的完整工具白名单。"""

    config = chat_plugin.plugin_config.model_copy(
        update={
            "dispatch_chat_mcp_enabled": True,
            "dispatch_chat_job_enabled": True,
            "dispatch_chat_tagged_memory_enabled": True,
            "dispatch_chat_mention_tool_enabled": True,
            "dispatch_chat_local_workflow_enabled": True,
            "dispatch_chat_tts_enabled": True,
        }
    )
    manager = ChatToolSessionManager(
        config_provider=lambda: config,
        store_provider=lambda: object(),
        naraka_service_provider=lambda: object(),
        group_member_refresher=lambda _bot, _group_id: None,
        workflow_service_provider=lambda: object(),
        tts_service_provider=lambda: object(),
        memory_store_provider=lambda: object(),
    )

    assert manager.available_group_tools() == (
        "job",
        "memory",
        "naraka_query",
        "naraka_search",
        "mention_member",
        "local_workflow_list",
        "local_workflow_generate_image",
        "tts_generate",
    )


@pytest.mark.asyncio
async def test_memory_identity_is_bound_in_direct_and_queued_sessions(chat_plugin, monkeypatch):
    """同步和队列入口只使用真实事件身份，关闭热配置后移除工具。"""
    from unittest.mock import Mock

    from agent_bot.services.conversation_queue import ConversationJob
    from agent_bot.services.tool_sessions import ToolSessionStore

    config = chat_plugin.plugin_config.model_copy(update={"dispatch_chat_mcp_enabled": True})
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=3)
    memory_store = Mock()
    bot = Mock(self_id="90001")
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: sessions,
        naraka_service_provider=lambda: None,
        group_member_refresher=lambda _bot, _group_id: None,
        memory_store_provider=lambda: memory_store,
    )
    assert manager.available_tools(_private_event()) == ("memory",)
    async with manager.open_chat_session(bot, _private_event(), "bound-session") as lease:
        context = await sessions.resolve(lease.token)
        assert context.memory_session.identity.bot_id == "90001"
        assert context.memory_session.identity.session_id == "bound-session"
        assert context.memory_session.identity.user_id == "10001"
        assert context.memory_session.identity.group_id is None
    monkeypatch.setattr(manager, "queued_bot", lambda _bot_id: bot)
    job = ConversationJob(
        job_id="memory-test", request_id="memory-test", created_at=1,
        session_id="group-session", bot_id="90001", scene="group", group_id="20000",
        user_id="10002", prompt="你好", chat_tools=("memory",),
    )
    async with manager.open_queued_session(job) as lease:
        context = await sessions.resolve(lease.token)
        assert context.memory_session.identity.session_id == "group-session"
        assert context.memory_session.identity.user_id == "10002"
        assert context.memory_session.identity.group_id == "20000"
    config = config.model_copy(update={"dispatch_chat_tagged_memory_enabled": False})
    assert manager.available_tools(_private_event()) == ()
    with pytest.raises(RuntimeError, match="configuration changed"):
        async with manager.open_queued_session(job):
            pytest.fail("a disabled tool must not open")


@pytest.mark.parametrize("configured,mcp", [(True, True), (False, True), (True, False)])
def test_image_management_remains_available_when_generation_is_disabled(
    chat_plugin, configured: bool, mcp: bool,
) -> None:
    """关闭生图后仍能管理已提交任务；没有服务或 MCP 权限时隐藏工具。"""

    config = chat_plugin.plugin_config.model_copy(update={
        "dispatch_chat_mcp_enabled": mcp,
        "dispatch_chat_local_workflow_enabled": False,
    })
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: object(),
        naraka_service_provider=lambda: None,
        group_member_refresher=lambda _bot, _group_id: None,
        image_tasks_provider=lambda: object() if configured else None,
    )
    expected = ("local_workflow_tasks", "local_workflow_cancel") if configured and mcp else ()
    assert manager.available_tools(_private_event()) == expected


async def test_image_management_binds_service_and_owner_for_direct_and_queued_sessions(
    chat_plugin, monkeypatch,
) -> None:
    """两种聊天入口的任务管理只能使用真实消息身份。"""

    from unittest.mock import Mock

    from agent_bot.services.conversation_queue import ConversationJob
    from agent_bot.services.tool_sessions import ToolSessionStore

    config = chat_plugin.plugin_config.model_copy(update={"dispatch_chat_mcp_enabled": True})
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=3)
    service = object()
    bot = Mock(self_id="90001")
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: sessions,
        naraka_service_provider=lambda: None,
        group_member_refresher=lambda _bot, _group_id: None,
        image_tasks_provider=lambda: service,
    )
    async with manager.open_chat_session(bot, _private_event(), "bound-session") as lease:
        context = await sessions.resolve(lease.token)
        assert context.image_tasks is service
        assert (context.bot_user_id, context.session_id, context.user_id) == (
            "90001", "bound-session", "10001",
        )
    monkeypatch.setattr(manager, "queued_bot", lambda _bot_id: bot)
    job = ConversationJob(
        job_id="management-test", request_id="management-test", created_at=1,
        session_id="group-session", bot_id="90001", scene="group", group_id="20000",
        user_id="10002", prompt="取消任务", chat_tools=("local_workflow_cancel",),
    )
    async with manager.open_queued_session(job) as lease:
        context = await sessions.resolve(lease.token)
        assert context.image_tasks is service
        assert (context.bot_user_id, context.session_id, context.user_id) == (
            "90001", "group-session", "10002",
        )
