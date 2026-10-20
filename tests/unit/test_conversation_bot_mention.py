"""纯机器人 At 必须还原真实名称并进入正常聊天队列。"""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.bot import _check_at_me
from nonebot.exception import FinishedException


def _group_event(message: Message) -> GroupMessageEvent:
    """保留适配器处理前的消息副本，模拟真实 OneBot 入站事件。"""

    return GroupMessageEvent(
        time=1,
        self_id=90001,
        post_type="message",
        sub_type="normal",
        user_id=10001,
        message_type="group",
        message_id=2,
        message=deepcopy(message),
        original_message=deepcopy(message),
        raw_message=str(message),
        font=0,
        sender={"user_id": 10001, "nickname": "测试用户", "role": "member"},
        group_id=20001,
        to_me=True,
    )


@pytest.mark.parametrize("adapter_preprocessed", [False, True])
@pytest.mark.parametrize(
    ("card", "nickname", "expected"),
    [("群里的桃桃", "QQ桃桃", "群里的桃桃"), ("", "QQ桃桃", "QQ桃桃")],
)
async def test_bare_bot_mention_queues_actual_member_name(
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
    adapter_preprocessed: bool,
    card: str,
    nickname: str,
    expected: str,
) -> None:
    """删掉触发 At 或用固定招呼替代名称，都会丢失应入队的用户输入。"""

    monkeypatch.setattr(
        chat_plugin, "_load_group_members", AsyncMock(return_value=[
            {"user_id": 90001, "card": card, "nickname": nickname},
        ]),
    )
    reply_model = AsyncMock(return_value="不应调用校验回复")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", reply_model)
    event = _group_event(Message([
        MessageSegment.at(90001), MessageSegment.text(" \t "),
    ]))
    bot = SimpleNamespace(self_id="90001")
    if adapter_preprocessed:
        _check_at_me(bot, event)
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))

    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            bot, event, matcher=matcher, mention_handler=None,
        )

    jobs = chat_plugin.conversation_queue.enqueue.await_args_list
    assert len(jobs) == 1
    assert jobs[0].args[0].prompt == expected
    assert jobs[0].args[0].mention_target_id is None
    reply_model.assert_not_awaited()
    matcher.send.assert_not_awaited()


async def test_bare_bot_mention_refreshes_missing_member_name(
    chat_plugin, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """旧目录没有机器人时，使用已有的一次刷新得到其真实名称。"""

    monkeypatch.setattr(chat_plugin, "_load_group_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        chat_plugin, "_refresh_group_members", AsyncMock(return_value=[
            {"user_id": 90001, "card": "最新群名片", "nickname": "QQ桃桃"},
        ]),
    )
    reply_model = AsyncMock(return_value="不应调用校验回复")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", reply_model)
    event = _group_event(Message(MessageSegment.at(90001)))
    bot = SimpleNamespace(self_id="90001")
    _check_at_me(bot, event)
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))

    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            bot, event, matcher=matcher, mention_handler=None,
        )

    jobs = chat_plugin.conversation_queue.enqueue.await_args_list
    assert len(jobs) == 1
    assert jobs[0].args[0].prompt == "最新群名片"
    reply_model.assert_not_awaited()
