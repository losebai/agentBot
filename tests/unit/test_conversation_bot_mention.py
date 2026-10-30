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


@pytest.mark.parametrize("prompt,members", [
    ("他没理你，at一下", []),
    ("帮我艾特一下", []),
    ("帮我at他一下", [{"user_id": 10086, "nickname": "小明", "card": ""}]),
    ("帮我艾特小明，集合", [
        {"user_id": 10086, "nickname": "小明", "card": ""},
        {"user_id": 10087, "nickname": "小明", "card": ""},
    ]),
])
async def test_contextual_mention_keeps_original_prompt_context_and_authorized_tools(
    chat_plugin, monkeypatch, prompt, members,
) -> None:
    """省略、指代和同名目标均进入原Agent队列，保留上下文与工具且不单开提示调用。"""
    tools = ("mention_member", "memory", "tts_generate")
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_auto_mention_enabled", True)
    monkeypatch.setattr(chat_plugin._tool_session_manager, "available_tools", lambda _: tools)
    monkeypatch.setattr(chat_plugin, "_load_group_members", AsyncMock(return_value=members))
    monkeypatch.setattr(chat_plugin, "_refresh_group_members", AsyncMock(return_value=members))
    counter = Mock()
    counter.try_consume.return_value = True
    monkeypatch.setattr(chat_plugin, "message_counter", counter)
    reply_model = AsyncMock(return_value="不应调用校验回复")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", reply_model)
    monkeypatch.setattr(chat_plugin._conversation_handler, "_recent_context_provider", AsyncMock(
        return_value="qwen3.8(QQ 10086)：刚才的消息",
    ))
    event = _group_event(Message(prompt))
    bot = SimpleNamespace(self_id="90001")
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))

    async def parse_mention(current_bot, current_event):
        """使用真实前置解析器，确认不能提前结束或缩减工具授权。"""
        return await chat_plugin._conversation_handler.conversational_mention(
            current_bot, current_event, matcher=matcher,
        )

    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            bot, event, matcher=matcher, mention_handler=parse_mention,
        )

    chat_plugin.conversation_queue.enqueue.assert_awaited_once()
    job = chat_plugin.conversation_queue.enqueue.await_args.args[0]
    assert job.prompt == prompt
    assert job.recent_group_context == "qwen3.8(QQ 10086)：刚才的消息"
    assert job.chat_tools == tools
    assert job.mention_target_id is None
    counter.try_consume.assert_called_once_with(str(event.user_id))
    reply_model.assert_not_awaited()
    matcher.send.assert_not_awaited()
