"""尚未实现的通用 Laya 命令应明确拒绝且不入队。"""

from unittest.mock import AsyncMock, Mock

import pytest
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    PrivateMessageEvent,
)
from nonebot.exception import FinishedException


def laya_event(argument: str, *, group: bool):
    """构造能被真实命令 Matcher 解析的群聊或私聊事件。"""
    text = f"/laya {argument}".strip()
    event_type = GroupMessageEvent if group else PrivateMessageEvent
    payload = {
        "time": 1, "self_id": 90001, "post_type": "message", "message_id": 1,
        "user_id": 10001, "message": Message(text), "raw_message": text, "font": 0,
        "sender": {"user_id": 10001, "nickname": "tester"},
        "message_type": "group" if group else "private",
        "sub_type": "normal" if group else "friend",
    }
    if group:
        payload["group_id"] = 20001
    return event_type.model_validate(payload)


async def test_disabled_laya_command_uses_llm_explanation(chat_plugin, monkeypatch) -> None:
    """未启用功能时仍由统一回复模型解释，不入队也不使用固定正文。"""
    compose = AsyncMock(return_value="模型生成的 Laya 未启用说明")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", compose)
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.laya_command(
            Mock(self_id="90001"), laya_event("退款", group=False), Message("退款"),
            matcher=matcher,
        )
    matcher.send.assert_awaited_once_with(Message("模型生成的 Laya 未启用说明"))
    assert "Laya" in compose.await_args.kwargs["instruction"]
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()
