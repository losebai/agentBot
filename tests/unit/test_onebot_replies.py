from __future__ import annotations

from dataclasses import dataclass

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from agent_bot.config import RuntimeConfig
from agent_bot.integrations.onebot.replies import (
    OneBotReplyGateway,
    OneBotReplyService,
)
from agent_bot.services.replies import (
    CommandUsageFacts,
    GeneratedReply,
    ReplyIntent,
)


def runtime_config(*, chunk_chars: int = 1200, max_reply_chars: int = 6000) -> RuntimeConfig:
    """Build a runtime configuration with explicit reply limits."""

    return RuntimeConfig(
        onebot_v11_access_token="onebot-token-that-is-at-least-32-characters",
        chat_session_secret="session-secret-that-is-at-least-32-characters",
        chat_chunk_chars=chunk_chars,
        chat_max_reply_chars=max_reply_chars,
    )


class FakeMatcher:
    """Capture matcher sends and message-free completion."""

    def __init__(self) -> None:
        """Initialize empty delivery state."""

        self.sent: list[Message] = []
        self.finished_without_message = False

    async def send(self, message: Message) -> None:
        """Record one delivered message in call order."""

        self.sent.append(message)

    async def finish(self) -> None:
        """Record that the matcher ended without an attached message."""

        self.finished_without_message = True


@dataclass(frozen=True)
class BotCall:
    """Represent one concrete fake OneBot target API call."""

    api: str
    target_id: int
    message: Message


class FakeBot:
    """Capture direct group and private OneBot method calls."""

    def __init__(self) -> None:
        """Initialize an empty ordered call list."""

        self.calls: list[BotCall] = []

    async def send_group_msg(self, *, group_id: int, message: Message) -> dict[str, int]:
        """Record one group message call."""

        self.calls.append(BotCall("send_group_msg", group_id, message))
        return {"message_id": len(self.calls)}

    async def send_private_msg(self, *, user_id: int, message: Message) -> dict[str, int]:
        """Record one private message call."""

        self.calls.append(BotCall("send_private_msg", user_id, message))
        return {"message_id": len(self.calls)}


class FakeComposer:
    """Return a controlled generated reply for reply-service tests."""

    def __init__(self, result: GeneratedReply | None) -> None:
        """Store the result and initialize the intent call log."""

        self.result = result
        self.calls: list[ReplyIntent] = []

    async def compose(self, intent: ReplyIntent) -> GeneratedReply | None:
        """Record the intent and return the configured result."""

        self.calls.append(intent)
        return self.result


def gateway(*, chunk_chars: int = 1200, max_reply_chars: int = 6000) -> OneBotReplyGateway:
    """Build a reply gateway with deterministic split limits."""

    config = runtime_config(
        chunk_chars=chunk_chars,
        max_reply_chars=max_reply_chars,
    )
    return OneBotReplyGateway(lambda: config)


def valid_usage_intent() -> ReplyIntent:
    """Build a valid command-usage reply intent."""

    return ReplyIntent(
        requestId="req-1",
        sessionId="group:100",
        code="COMMAND_USAGE",
        facts=CommandUsageFacts(command="/画图", usage="/画图 <提示词>"),
    )


@pytest.mark.asyncio
async def test_gateway_rejects_plain_text() -> None:
    """A plain string can never enter the user-visible text gateway."""

    with pytest.raises(TypeError, match="GeneratedReply"):
        await gateway().send_matcher(FakeMatcher(), "fixed text")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_service_finishes_without_message_when_llm_fails() -> None:
    """A missing composed reply ends the matcher without fallback text."""

    matcher = FakeMatcher()
    composer = FakeComposer(result=None)
    service = OneBotReplyService(composer, gateway())  # type: ignore[arg-type]

    with pytest.raises(AssertionError, match=r"matcher\.finish must stop execution"):
        await service.finish_matcher(matcher, valid_usage_intent())

    assert composer.calls == [valid_usage_intent()]
    assert matcher.sent == []
    assert matcher.finished_without_message is True


@pytest.mark.asyncio
async def test_gateway_preserves_real_at_before_llm_text() -> None:
    """A real at segment precedes only the first generated text message."""

    matcher = FakeMatcher()
    reply = GeneratedReply(text="收到。", requestId="req-1")

    await gateway().send_matcher(
        matcher,
        reply,
        prefix=MessageSegment.at("123456"),
    )

    assert matcher.sent[0][0].type == "at"
    assert matcher.sent[0][0].data["qq"] == "123456"
    assert matcher.sent[0][1].type == "text"
    assert matcher.sent[0][1].data["text"] == " 收到。"


@pytest.mark.asyncio
@pytest.mark.parametrize("native_mention", [False, True])
async def test_gateway_never_turns_internal_at_text_into_platform_mentions(native_mention) -> None:
    """照抄的历史标记应移除，原生 @ 只能来自程序已授权的目标。"""
    matcher = FakeMatcher()
    reply = GeneratedReply(text="[AT 3616076808] 说得对。", requestId="auto-at")
    await gateway().send_matcher(
        matcher, reply, prefix=MessageSegment.at("123456") if native_mention else None,
    )
    message = matcher.sent[0]
    assert message.extract_plain_text().strip() == "说得对。"
    assert [segment.data["qq"] for segment in message if segment.type == "at"] == (
        ["123456"] if native_mention else []
    )


@pytest.mark.asyncio
async def test_mention_gateway_builds_at_in_transport_and_preserves_response() -> None:
    """Business tools provide a QQ target, while the transport owns native segments."""

    bot = FakeBot()
    response = await gateway().send_mention(
        bot, target_id="10001", member_id=123456,
        reply=GeneratedReply(text="模型提醒", requestId="req-mention"),
    )

    assert response == {"message_id": 1}
    assert bot.calls[0].message[0] == MessageSegment.at(123456)
    assert bot.calls[0].message.extract_plain_text() == " 模型提醒"


@pytest.mark.asyncio
async def test_gateway_splits_in_order_and_prefixes_only_first_part() -> None:
    """Current runtime limits split generated text without reordering parts."""

    matcher = FakeMatcher()
    reply = GeneratedReply(text="abcdefg", requestId="req-split")

    await gateway(chunk_chars=3, max_reply_chars=20).send_matcher(
        matcher,
        reply,
        prefix=MessageSegment.at("123456"),
    )

    assert [segment.type for segment in matcher.sent[0]] == ["at", "text"]
    assert matcher.sent[0][1].data["text"] == " abc"
    assert [message[0].data["text"] for message in matcher.sent[1:]] == ["def", "g"]


@pytest.mark.asyncio
async def test_gateway_uses_group_and_private_target_apis_in_order() -> None:
    """Direct bot delivery selects the requested target API for each scene."""

    bot = FakeBot()
    reply = GeneratedReply(text="abcdef", requestId="req-target")

    group_response = await gateway(chunk_chars=3).send_bot(
        bot,  # type: ignore[arg-type]
        scene="group",
        target_id="10001",
        reply=reply,
    )
    private_response = await gateway(chunk_chars=10).send_bot(
        bot,  # type: ignore[arg-type]
        scene="private",
        target_id="20002",
        reply=GeneratedReply(text="private", requestId="req-private"),
    )

    assert [(call.api, call.target_id) for call in bot.calls] == [
        ("send_group_msg", 10001),
        ("send_group_msg", 10001),
        ("send_private_msg", 20002),
    ]
    assert [call.message[0].data["text"] for call in bot.calls] == [
        "abc",
        "def",
        "private",
    ]
    assert group_response.response == {"message_id": 2}
    assert group_response.text == "abcdef"
    assert private_response.response == {"message_id": 3}
    assert private_response.text == "private"


@pytest.mark.asyncio
async def test_exact_bot_part_is_not_resplit_after_config_change() -> None:
    """A persisted queue part remains one delivery after reply limits hot-reload."""

    current_config = runtime_config(chunk_chars=10, max_reply_chars=20)
    reply_gateway = OneBotReplyGateway(lambda: current_config)
    bot = FakeBot()
    current_config = runtime_config(chunk_chars=1, max_reply_chars=1)

    response = await reply_gateway.send_bot_exact_part(
        bot,  # type: ignore[arg-type]
        scene="group",
        target_id="10001",
        reply=GeneratedReply(text="persisted-part", requestId="req-queue"),
        prefix=MessageSegment.at("30003"),
    )

    assert len(bot.calls) == 1
    assert bot.calls[0].api == "send_group_msg"
    assert [segment.type for segment in bot.calls[0].message] == ["at", "text"]
    assert bot.calls[0].message[1].data["text"] == " persisted-part"
    assert response == {"message_id": 1}


@pytest.mark.asyncio
async def test_exact_bot_part_rejects_plain_text() -> None:
    """Queue delivery cannot bypass generated-reply provenance validation."""

    with pytest.raises(TypeError, match="GeneratedReply"):
        await gateway().send_bot_exact_part(
            FakeBot(),  # type: ignore[arg-type]
            scene="private",
            target_id="20002",
            reply="persisted-part",  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_gateway_rejects_text_segment_as_prefix() -> None:
    """A text MessageSegment cannot bypass the GeneratedReply requirement."""

    with pytest.raises(TypeError, match="at"):
        await gateway().send_matcher(
            FakeMatcher(),
            GeneratedReply(text="generated", requestId="req-prefix"),
            prefix=MessageSegment.text("fixed prefix"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "segment",
    [
        MessageSegment.image("base64://aW1hZ2U="),
        MessageSegment.record("base64://YXVkaW8="),
        MessageSegment.video("https://example.invalid/video.mp4"),
    ],
)
async def test_gateway_sends_supported_structured_media(segment: MessageSegment) -> None:
    """Supported media remains a structured OneBot segment."""

    matcher = FakeMatcher()

    await gateway().send_media_matcher(matcher, segment)

    assert matcher.sent == [Message(segment)]


@pytest.mark.asyncio
async def test_media_gateway_rejects_text_segment() -> None:
    """The media path cannot carry user-visible text segments."""

    with pytest.raises(ValueError, match="unsupported media segment"):
        await gateway().send_media_matcher(
            FakeMatcher(),
            MessageSegment.text("fixed text"),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("scene", ["group", "private"])
@pytest.mark.parametrize("segment", [
    MessageSegment.image("base64://aW1hZ2U="),
    MessageSegment.record("base64://YXVkaW8="),
    MessageSegment.video("https://example.invalid/video.mp4"),
])
async def test_bot_media_gateway_preserves_target_and_at_order(scene, segment) -> None:
    """Structured bot delivery preserves media type, target and preceding real At."""

    bot = FakeBot()
    response = await gateway().send_media_bot(
        bot, scene=scene, target_id="10001", segment=segment,
        prefix=MessageSegment.at("20002"),
    )

    assert response == {"message_id": 1}
    assert bot.calls == [BotCall(
        f"send_{scene}_msg", 10001, Message([MessageSegment.at("20002"), segment]),
    )]


@pytest.mark.asyncio
async def test_matcher_media_gateway_preserves_real_at_order() -> None:
    """Matcher media delivery carries the original real At before its media segment."""

    matcher = FakeMatcher()
    segment = MessageSegment.image("base64://aW1hZ2U=")
    await gateway().send_media_matcher(matcher, segment, prefix=MessageSegment.at("20002"))

    assert matcher.sent == [Message([MessageSegment.at("20002"), segment])]


@pytest.mark.asyncio
@pytest.mark.parametrize("segment,prefix,error", [
    (MessageSegment.text("fixed"), None, ValueError),
    (MessageSegment.image("base64://aW1hZ2U="), MessageSegment.text("fixed"), TypeError),
])
async def test_bot_media_gateway_rejects_text_in_media_or_prefix(segment, prefix, error) -> None:
    """Neither a media segment nor its prefix can smuggle fixed text to the bot."""

    bot = FakeBot()
    with pytest.raises(error):
        await gateway().send_media_bot(
            bot, scene="group", target_id="10001", segment=segment, prefix=prefix,
        )
    assert bot.calls == []


@pytest.mark.asyncio
async def test_service_sends_composed_reply_and_reports_delivery() -> None:
    """The reply service composes one intent and reports a successful send."""

    matcher = FakeMatcher()
    composer = FakeComposer(
        GeneratedReply(text="generated", requestId="req-1"),
    )
    service = OneBotReplyService(composer, gateway())  # type: ignore[arg-type]

    sent = await service.send_matcher(matcher, valid_usage_intent())

    assert sent is True
    assert composer.calls == [valid_usage_intent()]
    assert matcher.sent == [Message(MessageSegment.text("generated"))]
