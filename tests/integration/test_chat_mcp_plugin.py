from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

from agent_bot.config import RuntimeConfig
from agent_bot.services.chat_tools import build_chat_tool_registry
from agent_bot.services.tool_sessions import ToolSessionExpiredError, ToolSessionStore


def group_event(message: str) -> GroupMessageEvent:
    content = Message(message)
    return GroupMessageEvent(
        time=1,
        self_id=90001,
        post_type="message",
        sub_type="normal",
        user_id=10001,
        message_type="group",
        message_id=2,
        message=content,
        original_message=content,
        raw_message=str(content),
        font=0,
        sender=Sender(user_id=10001, nickname="tester", role="member"),
        group_id=20001,
        to_me=True,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_tool_request_context_is_available_only_during_the_ai_call(
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    monkeypatch.setattr(chat_plugin, "_tool_session_store", store)
    monkeypatch.setattr(chat_plugin, "_tool_registry", build_chat_tool_registry())
    monkeypatch.setattr(chat_plugin, "naraka_service", AsyncMock())
    config_values = chat_plugin.plugin_config.model_dump()
    config_values["dispatch_chat_mcp_enabled"] = True
    config_values["dispatch_chat_mention_tool_enabled"] = True
    monkeypatch.setattr(chat_plugin, "plugin_config", RuntimeConfig.model_validate(config_values))
    bot = AsyncMock(spec=Bot)
    bot.self_id = "90001"
    event = group_event("查询永劫并提醒小明")

    async with chat_plugin._open_chat_tool_session(
        bot, event, "group-session"
    ) as (tools, token):
        assert tools == (
            "memory", "naraka_query", "naraka_search", "mention_member",
            "local_workflow_tasks", "local_workflow_cancel",
        )
        assert token is not None
        context = await store.resolve(token)
        assert context.group_id == 20001
        assert context.user_id == "10001"
        assert context.memory_session is not None
        assert context.memory_session.identity.bot_id == "90001"
        assert context.image_tasks is chat_plugin.image_queue.image_tasks

    assert token is not None
    with pytest.raises(ToolSessionExpiredError):
        await store.resolve(token)
