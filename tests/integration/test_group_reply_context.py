"""确认生产装配将真实已发送群文本并入近期快照，停用配置不会额外写入。"""

from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Message
from tests.unit.test_outgoing_context import Journal

from agent_bot.group_context import RecentGroupContextStore


@pytest.mark.parametrize(
    "enabled,nickname", [(True, "桃桃"), (False, "桃桃"), (True, "长" * 200)],
    ids=["enabled", "disabled", "long-name"],
)
async def test_host_projects_confirmed_group_reply_only_when_enabled(
    chat_plugin, monkeypatch, enabled: bool, nickname: str,
) -> None:
    """不访问真实数据库或 QQ，生产回调仍使用真实线程写入和群配置边界。"""
    recent = RecentGroupContextStore(message_limit=10, max_chars=1000,
                                    ttl_seconds=60, max_groups=10)
    monkeypatch.setattr(chat_plugin, "group_recent_context_store", recent)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_group_recent_context_enabled", enabled)
    monkeypatch.setattr(chat_plugin.plugin_config, "nickname", {nickname})
    recorder = chat_plugin._outgoing_recorder
    journal = Journal()
    monkeypatch.setattr(recorder, "store_provider", lambda: journal)
    monkeypatch.setattr(recorder, "receipts_provider", None)
    sender = AsyncMock(return_value={"message_id": 31})
    async with recorder.scope(
        "group-test-session", "confirmed-answer", key="part", generation_id="generation",
        bot_id="90001", group_id="20001",
    ):
        await recorder.send(Message("这是已经发送的回答。"), sender, key="text")
    sender.assert_awaited_once()
    context = recent.context(bot_id="90001", group_id="20001")
    if enabled:
        name = recent.snapshot(bot_id="90001", group_id="20001").latest_message.sender_name
        assert name.startswith("机器人[")
        assert len(name.encode("utf-8")) <= 512
        if nickname == "桃桃":
            assert "机器人[桃桃]" in context
        assert "这是已经发送的回答。" in context
    else:
        assert context is None
