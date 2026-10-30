from __future__ import annotations

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from agent_bot.mentions import (
    AmbiguousMentionTargetError,
    InvalidGroupMemberDataError,
    InvalidMentionCommandError,
    InvalidMentionDirectiveError,
    MentionCommand,
    MentionTargetNotFoundError,
    NamedMentionRequest,
    build_mention_message,
    parse_conversational_mention,
    parse_mention_command,
    resolve_named_mention,
    resolve_tool_mention,
)


def test_parse_numeric_qq_and_build_real_onebot_at_segment() -> None:
    command = parse_mention_command(Message("123456789  请来开黑\n别迟到 "))

    assert command.target_user_id == 123456789
    assert command.text == "请来开黑 别迟到"
    assert build_mention_message(command) == Message(
        [
            MessageSegment.at(123456789),
            MessageSegment.text(" 请来开黑 别迟到"),
        ]
    )


def test_parse_native_at_segment_as_target() -> None:
    argument = Message(
        [
            MessageSegment.text("  "),
            MessageSegment.at(10086),
            MessageSegment.text(" 该集合了 "),
        ]
    )

    command = parse_mention_command(argument)

    assert command.target_user_id == 10086
    assert command.text == "该集合了"


def test_parse_conversational_native_at_directive_ignores_bot_mention() -> None:
    message = Message(
        [
            MessageSegment.at(90001),
            MessageSegment.text(" 帮我 "),
            MessageSegment.at(10086),
            MessageSegment.text(" 一下，告诉他该集合了 "),
        ]
    )

    command = parse_conversational_mention(message, bot_user_id=90001)

    assert command == MentionCommand(target_user_id=10086, text="告诉他该集合了")


def test_parse_conversational_numeric_directive_without_native_segment() -> None:
    command = parse_conversational_mention(
        Message("我让你@10086，提醒他开会"),
        bot_user_id=90001,
    )

    assert command == MentionCommand(target_user_id=10086, text="提醒他开会")


@pytest.mark.parametrize("prompt", ["他没理你，at一下", "帮我艾特一下", "at"])
def test_contextual_mention_without_explicit_target_is_left_to_agent(prompt: str) -> None:
    """目标省略属于需要上下文的正常对话，不能被解释为非法提醒参数。"""
    assert parse_conversational_mention(Message(prompt), bot_user_id=90001) is None


def test_conversational_marker_is_recognized_anywhere_in_first_twenty_characters() -> None:
    named_request = parse_conversational_mention(
        Message("桃桃，at一下小翊，告诉他没事多喝水，没钱就"),
        bot_user_id=90001,
    )

    assert isinstance(named_request, NamedMentionRequest)
    assert resolve_named_mention(
        named_request,
        [{"user_id": 10086, "nickname": "小翊", "card": ""}],
        bot_user_id=90001,
    ) == MentionCommand(target_user_id=10086, text="告诉他没事多喝水,没钱就")
    assert parse_conversational_mention(
        Message("桃桃，帮我艾特一下1145000687，提醒他该睡觉了"),
        bot_user_id=90001,
    ) == MentionCommand(target_user_id=1145000687, text="提醒他该睡觉了")
    assert parse_conversational_mention(
        Message(f"{'甲' * 19}at一下10086，集合"),
        bot_user_id=90001,
    ) == MentionCommand(target_user_id=10086, text="集合")


@pytest.mark.parametrize(
    "message",
    [
        Message(f"{'甲' * 20}at一下10086，集合"),
        Message("先说其他事情。at一下10086，集合"),
        Message("status update 10086，集合"),
    ],
)
def test_conversational_marker_outside_first_sentence_window_is_ignored(
    message: Message,
) -> None:
    assert parse_conversational_mention(message, bot_user_id=90001) is None


def test_named_directive_resolves_unique_longest_group_card() -> None:
    request = parse_conversational_mention(
        Message("请你艾特一下技术部小明，告诉他看群消息"),
        bot_user_id=90001,
    )

    assert isinstance(request, NamedMentionRequest)
    command = resolve_named_mention(
        request,
        [
            {"user_id": 10010, "nickname": "小明", "card": ""},
            {"user_id": 10086, "nickname": "李明", "card": "技术部小明"},
        ],
        bot_user_id=90001,
    )

    assert command == MentionCommand(target_user_id=10086, text="告诉他看群消息")


def test_named_directive_uses_safe_default_text() -> None:
    request = parse_conversational_mention(
        Message("麻烦你艾特小明"),
        bot_user_id=90001,
    )

    assert isinstance(request, NamedMentionRequest)
    command = resolve_named_mention(
        request,
        [{"user_id": 10086, "nickname": "小明", "card": ""}],
        bot_user_id=90001,
    )

    assert command == MentionCommand(target_user_id=10086, text="有人找你。")


def test_passive_native_mention_is_not_a_delivery_directive() -> None:
    message = Message(
        [
            MessageSegment.at(10086),
            MessageSegment.text("，你去叫一下桃桃"),
        ]
    )

    assert parse_conversational_mention(message, bot_user_id=90001) is None


def test_explicit_native_mention_action_still_builds_delivery_directive() -> None:
    message = Message(
        [
            MessageSegment.text("桃桃，帮我 "),
            MessageSegment.at(10086),
            MessageSegment.text(" 一下，告诉他没事多喝水"),
        ]
    )

    assert parse_conversational_mention(message, bot_user_id=90001) == MentionCommand(
        target_user_id=10086,
        text="告诉他没事多喝水",
    )


def test_named_directive_rejects_ambiguous_or_missing_group_member() -> None:
    request = parse_conversational_mention(
        Message("帮我艾特小明，集合"),
        bot_user_id=90001,
    )
    assert isinstance(request, NamedMentionRequest)

    with pytest.raises(AmbiguousMentionTargetError):
        resolve_named_mention(
            request,
            [
                {"user_id": 10010, "nickname": "小明", "card": ""},
                {"user_id": 10086, "nickname": "小明", "card": ""},
            ],
            bot_user_id=90001,
        )
    with pytest.raises(MentionTargetNotFoundError):
        resolve_named_mention(
            request,
            [{"user_id": 10010, "nickname": "小红", "card": ""}],
            bot_user_id=90001,
        )


def test_named_directive_bounds_and_validates_group_member_results() -> None:
    request = NamedMentionRequest(target_and_text="小明，集合")

    with pytest.raises(InvalidGroupMemberDataError):
        resolve_named_mention(request, {"not": "a list"}, bot_user_id=90001)
    with pytest.raises(InvalidGroupMemberDataError):
        resolve_named_mention(
            request,
            [
                {"user_id": 10000 + index, "nickname": f"成员{index}", "card": ""}
                for index in range(2001)
            ],
            bot_user_id=90001,
        )


def test_conversational_directive_rejects_multiple_or_all_mentions() -> None:
    with pytest.raises(InvalidMentionDirectiveError):
        parse_conversational_mention(
            Message(
                [
                    MessageSegment.text("帮我 "),
                    MessageSegment.at(10086),
                    MessageSegment.at(10010),
                    MessageSegment.text(" 集合"),
                ]
            ),
            bot_user_id=90001,
        )
    with pytest.raises(InvalidMentionDirectiveError):
        parse_conversational_mention(
            Message(
                [
                    MessageSegment.text("帮我 "),
                    MessageSegment.at("all"),
                    MessageSegment.text(" 集合"),
                ]
            ),
            bot_user_id=90001,
        )


@pytest.mark.parametrize(
    "argument",
    [
        Message("all 全体集合"),
        Message("01234 非法目标"),
        Message("1234 位数太短"),
        Message("123456789"),
        Message([MessageSegment.at("all"), MessageSegment.text(" 禁止全体提醒")]),
        Message(
            [
                MessageSegment.at(10086),
                MessageSegment.at(10010),
                MessageSegment.text(" 多个目标"),
            ]
        ),
        Message(
            [
                MessageSegment.at(10086),
                MessageSegment.image("https://example.invalid/image.png"),
            ]
        ),
        Message(f"10086 {'x' * 501}"),
    ],
)
def test_parse_rejects_unsafe_or_incomplete_mentions(argument: Message) -> None:
    with pytest.raises(InvalidMentionCommandError):
        parse_mention_command(argument)


def test_resolve_tool_mention_requires_a_current_group_member() -> None:
    members = [
        {"user_id": 10001, "nickname": "发起人", "card": ""},
        {"user_id": 10086, "nickname": "小明", "card": "技术部小明"},
        {"user_id": 90001, "nickname": "机器人", "card": ""},
    ]

    assert resolve_tool_mention(
        "10086", "集合", members, bot_user_id=90001
    ) == MentionCommand(target_user_id=10086, text="集合")
    assert resolve_tool_mention(
        "技术部小明", "请看群消息", members, bot_user_id=90001
    ) == MentionCommand(target_user_id=10086, text="请看群消息")
    assert resolve_tool_mention(
        "10001", "集合", members, bot_user_id=90001
    ) == MentionCommand(target_user_id=10001, text="集合")

    with pytest.raises(MentionTargetNotFoundError):
        resolve_tool_mention(
            "10010", "集合", members, bot_user_id=90001
        )
    with pytest.raises(InvalidMentionDirectiveError):
        resolve_tool_mention(
            "90001", "集合", members, bot_user_id=90001
        )
