from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from workflow_fakes import TextWorkflowServiceStub

from agent_bot.config import RuntimeConfig
from agent_bot.integrations.gpt_sovits import GeneratedSpeech, GptSoVitsError
from agent_bot.integrations.onebot.replies import OneBotReplyGateway
from agent_bot.integrations.workflow_store import WorkflowNotFoundError
from agent_bot.services.chat_tools import (
    ChatToolContext,
    ChatToolDefinition,
    ChatToolError,
    ChatToolRegistry,
    SpeechCollector,
    build_chat_tool_registry,
)
from agent_bot.services.naraka import (
    NarakaPlayerSearchResult,
    NarakaResult,
)
from agent_bot.services.replies import LlmReplyComposer
from agent_bot.services.workflow_execution import (
    GeneratedWorkflowImage,
    WorkflowExecutionError,
    WorkflowImageCollector,
    WorkflowSummary,
)


class MentionBot:
    """Keep offline API assertions behind the concrete gateway API."""

    def __init__(self, call_api) -> None:
        """Capture member resolution and delivery through an offline adapter."""

        self.call_api = AsyncMock(side_effect=call_api)

    async def send_group_msg(self, **data: object) -> object:
        """Forward gateway delivery into the external API recorder."""

        return await self.call_api("send_group_msg", **data)


def mention_composer(answer: str = "请看群消息") -> LlmReplyComposer:
    """Supply offline model output through the real Composer."""

    chat = SimpleNamespace(generate_reply_queued=AsyncMock(return_value=answer))
    return LlmReplyComposer(lambda: chat, timeout_seconds_provider=lambda: 300)


def mention_gateway() -> OneBotReplyGateway:
    """Construct the real gateway with safe test configuration."""

    config = RuntimeConfig(
        onebot_v11_access_token="test-token-that-is-longer-than-32-characters",
        chat_session_secret="test-secret-that-is-longer-than-32-characters",
    )
    return OneBotReplyGateway(lambda: config)


def test_registry_rejects_duplicate_names_and_filters_the_allow_list() -> None:
    async def handler(arguments: dict[str, object], context: ChatToolContext) -> object:
        return {"ok": True}

    definition = ChatToolDefinition(
        name="example",
        description="example tool",
        input_schema={"type": "object", "properties": {}},
        handler=handler,
    )

    with pytest.raises(ValueError, match="duplicate"):
        ChatToolRegistry([definition, definition])

    registry = ChatToolRegistry([definition])
    assert registry.list_tools(frozenset({"example"})) == [definition]
    assert registry.list_tools(frozenset({"other"})) == []


def test_naraka_tool_descriptions_distinguish_details_from_candidate_search() -> None:
    registry = build_chat_tool_registry()
    definitions = {
        definition.name: definition
        for definition in registry.list_tools(frozenset({"naraka_query", "naraka_search"}))
    }

    assert definitions["naraka_query"].description == (
        "查询具体永劫无间角色的完整战绩详情，包括段位、评分、统计指标和最近对局。"
        "用户提供角色 ID 或昵称并询问战绩时必须使用本工具。"
    )
    assert definitions["naraka_search"].description == (
        "仅按昵称搜索永劫无间候选角色，最多返回 10 条；"
        "不能代替 naraka_query 查询具体角色的战绩。"
    )


@pytest.mark.asyncio
async def test_registry_rejects_unknown_or_unauthorized_tools() -> None:
    registry = build_chat_tool_registry()
    context = ChatToolContext(
        session_id="private:1",
        user_id="1",
        allowed_tools=frozenset({"naraka_query"}),
    )

    with pytest.raises(ChatToolError) as unknown:
        await registry.call("unknown", {}, context)
    assert unknown.value.code == "NOT_FOUND"

    with pytest.raises(ChatToolError) as forbidden:
        await registry.call("mention_member", {"target": "10086", "text": "集合"}, context)
    assert forbidden.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_naraka_query_accepts_exactly_one_role_id_or_nickname() -> None:
    service = type(
        "NarakaService",
        (),
        {
            "query": AsyncMock(
                return_value=NarakaResult.model_validate(
                    {"player_info": {"name": "测试玩家"}, "season": "qianji"}
                )
            ),
            "query_by_nickname": AsyncMock(
                return_value=NarakaResult.model_validate(
                    {"player_info": {"name": "昵称玩家"}}
                )
            ),
        },
    )()
    registry = build_chat_tool_registry()
    context = ChatToolContext(
        session_id="group:123",
        user_id="1",
        allowed_tools=frozenset({"naraka_query"}),
        naraka_service=service,
    )

    role_result = await registry.call(
        "naraka_query", {"role_id": "7pra000013743400100163"}, context
    )
    nickname_result = await registry.call(
        "naraka_query", {"nickname": "白小痴"}, context
    )

    assert json.loads(role_result.content)["player_info"]["name"] == "测试玩家"
    assert json.loads(nickname_result.content)["player_info"]["name"] == "昵称玩家"
    service.query.assert_awaited_once_with("group:123", "7pra000013743400100163")
    service.query_by_nickname.assert_awaited_once_with("group:123", "白小痴")

    for arguments in ({}, {"role_id": "1", "nickname": "x"}):
        with pytest.raises(ChatToolError) as invalid:
            await registry.call("naraka_query", arguments, context)
        assert invalid.value.code == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_naraka_search_returns_bounded_typed_results() -> None:
    players = [
        {
            "name": f"玩家{index}",
            "game_id": f"7pra{index:018d}",
            "server": "163",
        }
        for index in range(20)
    ]
    service = type(
        "NarakaService",
        (),
        {
            "search_players": AsyncMock(
                return_value=NarakaPlayerSearchResult(query="玩家", players=players)
            )
        },
    )()
    registry = build_chat_tool_registry()
    context = ChatToolContext(
        session_id="group:123",
        user_id="1",
        allowed_tools=frozenset({"naraka_search"}),
        naraka_service=service,
    )

    result = await registry.call("naraka_search", {"nickname": "玩家"}, context)

    assert len(json.loads(result.content)["players"]) == 10
    with pytest.raises(ChatToolError) as invalid:
        await registry.call("naraka_search", {"nickname": "x" * 33}, context)
    assert invalid.value.code == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_mention_member_selects_group_reply_idempotently() -> None:
    """提醒目标经过校验，正文仅登记到共享回复收集器。"""

    members = [
        {"user_id": 10001, "nickname": "发起人", "card": ""},
        {"user_id": 10086, "nickname": "小明", "card": "技术部小明"},
        {"user_id": 90001, "nickname": "机器人", "card": ""},
    ]

    async def call_api(api: str, **data: object) -> object:
        """Return members or verify a generated native at delivery offline."""

        if api == "get_group_member_list":
            assert data["group_id"] == 123
            return members
        if api == "send_group_msg":
            assert data["group_id"] == 123
            return {"message_id": 456}
        raise AssertionError(api)

    bot = MentionBot(call_api)
    registry = build_chat_tool_registry()
    context = ChatToolContext(
        session_id="group:123",
        user_id="10001",
        group_id=123,
        bot_user_id="90001",
        bot=bot,
        allowed_tools=frozenset({"mention_member"}),
    )

    first = await registry.call(
        "mention_member", {"target": "技术部小明", "text": "请看群消息"}, context
    )
    second = await registry.call(
        "mention_member", {"target": "技术部小明", "text": "请看群消息"}, context
    )

    assert json.loads(first.content) == {"status": "queued", "target_user_id": 10086}
    assert second == first
    assert bot.call_api.await_count == 1
    assert context.reply_collector.reply.text == "请看群消息"

    with pytest.raises(ChatToolError) as repeated_side_effect:
        await registry.call(
            "mention_member", {"target": "10086", "text": "另一条消息"}, context
        )
    assert repeated_side_effect.value.code == "EXECUTION_UNCERTAIN"


@pytest.mark.asyncio
async def test_mention_member_uses_member_provider_and_stages_native_at() -> None:
    """提醒目标经过校验，正文仅登记到共享回复收集器。"""

    members = [
        {"user_id": 10001, "nickname": "发起人", "card": ""},
        {"user_id": 10086, "nickname": "小明", "card": "技术部小明"},
    ]
    member_provider = AsyncMock(return_value=members)

    async def call_api(api: str, **data: object) -> object:
        """Return members or verify a generated native at delivery offline."""

        assert api == "send_group_msg"
        assert data == {
            "group_id": 123,
            "message": Message(
                [
                    MessageSegment.at(10086),
                    MessageSegment.text(" 请看群消息"),
                ]
            ),
        }
        return {"message_id": 456}

    bot = MentionBot(call_api)
    context = ChatToolContext(
        session_id="group:123",
        user_id="10001",
        group_id=123,
        bot_user_id="90001",
        bot=bot,
        group_member_provider=member_provider,
        allowed_tools=frozenset({"mention_member"}),
    )

    result = await build_chat_tool_registry().call(
        "mention_member",
        {"target": "技术部小明", "text": "请看群消息"},
        context,
    )

    assert json.loads(result.content) == {"status": "queued", "target_user_id": 10086}
    member_provider.assert_awaited_once_with()
    bot.call_api.assert_not_awaited()
    assert context.reply_collector.reply.target_user_id == 10086


@pytest.mark.asyncio
async def test_concurrent_identical_mentions_select_only_once() -> None:
    """提醒目标经过校验，正文仅登记到共享回复收集器。"""

    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def call_api(api: str, **data: object) -> object:
        """Return members or verify a generated native at delivery offline."""

        if api == "get_group_member_list":
            send_started.set()
            await release_send.wait()
            return [{"user_id": 10086, "nickname": "小明", "card": ""}]
        raise AssertionError(api)

    bot = MentionBot(call_api)
    context = ChatToolContext(
        session_id="group:123",
        user_id="10001",
        group_id=123,
        bot_user_id="90001",
        bot=bot,
        allowed_tools=frozenset({"mention_member"}),
    )
    registry = build_chat_tool_registry()
    arguments = {"target": "小明", "text": "请看群消息"}

    first = asyncio.create_task(registry.call("mention_member", arguments, context))
    await send_started.wait()
    second = asyncio.create_task(registry.call("mention_member", arguments, context))
    await asyncio.sleep(0)
    release_send.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert first_result == second_result
    assert sum(call.args[0] == "send_group_msg" for call in bot.call_api.await_args_list) == 0
    assert context.reply_collector.reply.text == "请看群消息"


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["10001", "bai(soloncode传道人)"])
async def test_mention_member_allows_requester_target(target: str) -> None:
    """提醒目标经过校验，正文仅登记到共享回复收集器。"""

    members = [
        {
            "user_id": 10001,
            "nickname": "bai",
            "card": "bai(soloncode传道人)",
        },
        {"user_id": 90001, "nickname": "桃桃", "card": ""},
    ]

    async def call_api(api: str, **data: object) -> object:
        """Return members or verify a generated native at delivery offline."""

        if api == "get_group_member_list":
            return members
        if api == "send_group_msg":
            assert data == {
                "group_id": 123,
                "message": Message(
                    [
                        MessageSegment.at(10001),
                        MessageSegment.text(" 哥哥看，桃桃@你啦~"),
                    ]
                ),
            }
            return {"message_id": 456}
        raise AssertionError(api)

    bot = MentionBot(call_api)
    context = ChatToolContext(
        session_id="group:123",
        user_id="10001",
        group_id=123,
        bot_user_id="90001",
        bot=bot,
        allowed_tools=frozenset({"mention_member"}),
    )

    result = await build_chat_tool_registry().call(
        "mention_member",
        {"target": target, "text": "哥哥看，桃桃@你啦~"},
        context,
    )

    assert json.loads(result.content) == {"status": "queued", "target_user_id": 10001}


@pytest.mark.asyncio
async def test_mention_member_rejects_private_or_bot_targets() -> None:
    registry = build_chat_tool_registry()
    private_context = ChatToolContext(
        session_id="private:10001",
        user_id="10001",
        allowed_tools=frozenset({"mention_member"}),
    )
    with pytest.raises(ChatToolError) as private:
        await registry.call(
            "mention_member", {"target": "10086", "text": "集合"}, private_context
        )
    assert private.value.code == "FORBIDDEN"

    bot = type(
        "Bot",
        (),
        {"call_api": AsyncMock(return_value=[{"user_id": 90001, "nickname": "桃桃"}])},
    )()
    group_context = ChatToolContext(
        session_id="group:123",
        user_id="10001",
        group_id=123,
        bot_user_id="90001",
        bot=bot,
        allowed_tools=frozenset({"mention_member"}),
    )
    with pytest.raises(ChatToolError) as bot_target:
        await registry.call(
            "mention_member", {"target": "90001", "text": "集合"}, group_context
        )
    assert bot_target.value.code == "FORBIDDEN"


@pytest.mark.asyncio
async def test_local_workflow_tools_list_and_collect_one_image() -> None:
    class WorkflowService(TextWorkflowServiceStub):
        async def list_workflows(self) -> tuple[WorkflowSummary, ...]:
            return (
                WorkflowSummary(
                    key="sdxl-lightning-4step",
                    name="SDXL Lightning",
                    description="适合快速生成写实图片",
                ),
            )

        async def generate(
            self,
            *,
            workflow_key: str,
            positive_prompt: str,
            negative_prompt: str,
        ) -> GeneratedWorkflowImage:
            marker = positive_prompt.encode("utf-8")
            return GeneratedWorkflowImage(
                media_type="image/png",
                data=b"\x89PNG\r\n\x1a\n" + marker,
            )

    registry = build_chat_tool_registry()
    collector = WorkflowImageCollector(max_total_bytes=1024)
    context = ChatToolContext(
        session_id="private:10001",
        user_id="10001",
        allowed_tools=frozenset(
            {"local_workflow_list", "local_workflow_generate_image"}
        ),
        workflow_service=WorkflowService(),
        workflow_collector=collector,
    )

    listed = await registry.call("local_workflow_list", {}, context)
    first = await registry.call(
        "local_workflow_generate_image",
        {"workflow_key": "sdxl-lightning-4step", "positive_prompt": "first",
         "negative_prompt": "blurry", "start_reply": "我来画。"},
        context,
    )
    assert json.loads(listed.content) == {
        "workflows": [
            {
                "key": "sdxl-lightning-4step",
                "name": "SDXL Lightning",
                "description": "适合快速生成写实图片",
                "workflow_type": "text_to_image",
                "requires_image": False,
            }
        ]
    }
    assert json.loads(first.content)["image_index"] == 1
    assert [image.data[-5:] for image in collector.images] == [b"first"]


def test_local_workflow_tool_requires_explicit_catalog_key() -> None:
    """工具 Schema 与说明都要求明确选取列表工作流，禁止默默回退默认配置。"""

    registry = build_chat_tool_registry()
    definitions = {
        definition.name: definition
        for definition in registry.list_tools(
            frozenset({"local_workflow_list", "local_workflow_generate_image"})
        )
    }

    assert "生成前必须先调用" in definitions["local_workflow_list"].description
    assert "不允许省略或回退" in definitions["local_workflow_generate_image"].description
    assert "workflow_key" in definitions["local_workflow_generate_image"].input_schema["required"]
    assert "最多生成一张" in definitions["local_workflow_generate_image"].description


def test_tts_generate_tool_exposes_bounded_text_contract() -> None:
    """缺少或放宽语音文本契约时应失败。"""

    registry = build_chat_tool_registry()
    definitions = {
        definition.name: definition
        for definition in registry.list_tools(frozenset({"tts_generate"}))
    }

    definition = definitions["tts_generate"]
    assert "GPT-SoVITS" in definition.description
    assert definition.input_schema["additionalProperties"] is False
    assert definition.input_schema["required"] == ["text"]
    assert definition.input_schema["properties"]["text"]["maxLength"] == 4096


@pytest.mark.asyncio
async def test_tts_generate_tool_returns_bounded_file_metadata() -> None:
    """工具直接泄漏对象或音频内容而非有界文件元数据时应失败。"""

    class RecordingSpeechCollector:
        """记录工具生成结果，模拟请求级语音投递收集器。"""

        def __init__(self) -> None:
            """初始化空的调用与语音记录。"""

            self.calls = 0
            self.items: list[tuple[str, GeneratedSpeech]] = []

        async def reserve_call(self) -> int:
            """记录一次生成调用。"""

            self.calls += 1
            return self.calls

        async def add(self, text: str, speech: GeneratedSpeech) -> None:
            """记录待发送的语音及其正文。"""

            self.items.append((text, speech))

    class TtsService:
        async def generate(self, text: str) -> GeneratedSpeech:
            assert text == "你好"
            return GeneratedSpeech(
                path=Path("G:/tts/voice.wav"),
                media_type="audio/wav",
                bytes=1234,
            )

    collector = RecordingSpeechCollector()
    context = ChatToolContext(
        session_id="private:10001",
        user_id="10001",
        allowed_tools=frozenset({"tts_generate"}),
        tts_service=TtsService(),
        tts_collector=collector,
    )

    result = await build_chat_tool_registry().call(
        "tts_generate",
        {"text": "你好"},
        context,
    )

    assert json.loads(result.content) == {
        "path": "G:/tts/voice.wav",
        "media_type": "audio/wav",
        "bytes": 1234,
    }
    assert collector.calls == 1
    assert collector.items == [
        (
            "你好",
            GeneratedSpeech(
                path=Path("G:/tts/voice.wav"),
                media_type="audio/wav",
                bytes=1234,
            ),
        )
    ]


@pytest.mark.asyncio
async def test_tts_generate_tool_removes_stage_directions_and_emoji() -> None:
    """括号语气和表情被原样发送给 GPT-SoVITS 时应失败。"""

    class TtsService:
        async def generate(self, text: str) -> GeneratedSpeech:
            assert text == "哥哥……"
            return GeneratedSpeech(
                path=Path("G:/tts/tone.wav"),
                media_type="audio/wav",
                bytes=321,
            )

    collector = SpeechCollector()
    context = ChatToolContext(
        session_id="private:10001",
        user_id="10001",
        allowed_tools=frozenset({"tts_generate"}),
        tts_service=TtsService(),
        tts_collector=collector,
    )

    await build_chat_tool_registry().call(
        "tts_generate",
        {"text": "\uff08小声\uff09 哥哥…… (轻笑) 😂"},
        context,
    )

    assert collector.speech is not None
    assert collector.speech.text == "哥哥……"


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["\u200b", "？！...😂"])
async def test_tts_generate_tool_rejects_text_without_speakable_characters(
    text: str,
) -> None:
    """不可见字符或纯符号绕过参数校验并请求上游时应失败。"""

    class UnexpectedTtsService:
        async def generate(self, _text: str) -> GeneratedSpeech:
            raise AssertionError("invalid text must not reach GPT-SoVITS")

    class UnexpectedSpeechCollector:
        async def reserve_call(self) -> int:
            raise AssertionError("invalid text must not consume the TTS call quota")

    context = ChatToolContext(
        session_id="private:10001",
        user_id="10001",
        allowed_tools=frozenset({"tts_generate"}),
        tts_service=UnexpectedTtsService(),
        tts_collector=UnexpectedSpeechCollector(),
    )

    with pytest.raises(ChatToolError) as failed:
        await build_chat_tool_registry().call(
            "tts_generate",
            {"text": text},
            context,
        )

    assert failed.value.code == "INVALID_ARGUMENT"


@pytest.mark.asyncio
async def test_tts_generate_tool_preserves_safe_service_error() -> None:
    """GPT-SoVITS 稳定错误码被错误改写为 INTERNAL 时应失败。"""

    class TtsService:
        async def generate(self, _text: str) -> object:
            raise GptSoVitsError("TIMEOUT", "GPT-SoVITS 请求超时")

    context = ChatToolContext(
        session_id="private:10001",
        user_id="10001",
        allowed_tools=frozenset({"tts_generate"}),
        tts_service=TtsService(),
        tts_collector=SpeechCollector(),
    )

    with pytest.raises(ChatToolError) as failed:
        await build_chat_tool_registry().call(
            "tts_generate",
            {"text": "你好"},
            context,
        )

    assert failed.value.code == "TIMEOUT"
    assert str(failed.value) == "GPT-SoVITS 请求超时"


@pytest.mark.asyncio
async def test_local_workflow_tool_hard_limits_calls() -> None:
    class WorkflowService(TextWorkflowServiceStub):
        async def generate(self, **_arguments: str) -> GeneratedWorkflowImage:
            return GeneratedWorkflowImage(
                media_type="image/png",
                data=b"\x89PNG\r\n\x1a\nimage",
            )

    registry = build_chat_tool_registry()
    context = ChatToolContext(
        session_id="private:10001",
        user_id="10001",
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=WorkflowService(),
        workflow_collector=WorkflowImageCollector(),
    )
    arguments = {
        "workflow_key": "sdxl-lightning-4step", "positive_prompt": "one",
        "negative_prompt": "", "start_reply": "我来画。",
    }
    await registry.call("local_workflow_generate_image", arguments, context)

    with pytest.raises(ChatToolError) as limited:
        await registry.call("local_workflow_generate_image", arguments, context)
    assert limited.value.code == "LIMIT_EXCEEDED"


async def test_workflow_tool_rejects_missing_key_before_using_disabled_default() -> None:
    """模型未选择工作流时不得悄悄使用已禁用默认项，也不得发送开画答复。"""

    generate, submit = AsyncMock(), AsyncMock(return_value="image-task")
    collector = WorkflowImageCollector()
    collector.submit = submit
    context = ChatToolContext(
        session_id="private:10001", user_id="10001",
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=SimpleNamespace(generate=generate), workflow_collector=collector,
    )
    with pytest.raises(ChatToolError) as invalid:
        await build_chat_tool_registry().call(
            "local_workflow_generate_image",
            {"positive_prompt": "cat", "start_reply": "我来画。"}, context,
        )
    assert invalid.value.code == "INVALID_ARGUMENT"
    assert "workflow_key" in str(invalid.value)
    generate.assert_not_awaited()
    submit.assert_not_awaited()


async def test_unavailable_workflow_has_specific_error_without_comfy_submission() -> None:
    """显式选择已禁用工作流时，返回可重新选择的错误而非笼统 INTERNAL。"""

    generate = AsyncMock(side_effect=WorkflowNotFoundError("PRIVATE_DATABASE_DETAIL"))
    context = ChatToolContext(
        session_id="private:10001", user_id="10001",
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=TextWorkflowServiceStub(
            generate=generate,
            describe_workflow=AsyncMock(side_effect=WorkflowNotFoundError("unavailable")),
        ),
        workflow_collector=WorkflowImageCollector(),
    )
    with pytest.raises(ChatToolError) as unavailable:
        await build_chat_tool_registry().call(
            "local_workflow_generate_image",
            {"workflow_key": "disabled-workflow", "positive_prompt": "cat",
             "start_reply": "我来画。"}, context,
        )
    assert unavailable.value.code == "WORKFLOW_UNAVAILABLE"
    assert "local_workflow_list" in str(unavailable.value)
    assert "PRIVATE_DATABASE_DETAIL" not in str(unavailable.value)


@pytest.mark.parametrize("start_reply", [None, "  ", "x" * 1001])
async def test_workflow_requires_valid_start_reply_before_any_side_effect(start_reply) -> None:
    """主模型遗漏开画答复或违反长度约束时，不提交任务也不调用补写模型。"""

    registry = build_chat_tool_registry()
    context = ChatToolContext(
        session_id="private:10001", user_id="10001",
        allowed_tools=frozenset({"local_workflow_generate_image"}),
    )
    arguments = {"workflow_key": "portrait", "positive_prompt": "cat"}
    if start_reply is not None:
        arguments["start_reply"] = start_reply
    with pytest.raises(ChatToolError) as invalid:
        await registry.call("local_workflow_generate_image", arguments, context)
    assert invalid.value.code == "INVALID_ARGUMENT"
    assert "start_reply" in str(invalid.value)


@pytest.mark.asyncio
async def test_local_workflow_tool_failure_logs_session_and_call_index(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MCP 工作流失败日志应能关联会话及请求内调用序号。"""

    class WorkflowService(TextWorkflowServiceStub):
        async def generate(self, **_arguments: str) -> GeneratedWorkflowImage:
            raise WorkflowExecutionError("EXECUTION_FAILED", "PRIVATE_COMFY_DETAIL")

    caplog.set_level(logging.WARNING, logger="agent_bot.services.chat_tools")
    registry = build_chat_tool_registry()
    context = ChatToolContext(
        session_id="qq_safe_session",
        user_id="10001",
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=WorkflowService(),
        workflow_collector=WorkflowImageCollector(),
    )

    with pytest.raises(ChatToolError) as failed:
        await registry.call(
            "local_workflow_generate_image",
            {
                "positive_prompt": "PRIVATE_POSITIVE_PROMPT",
                "workflow_key": "image-z-image-int8",
                "start_reply": "我来画。",
                "negative_prompt": "PRIVATE_NEGATIVE_PROMPT",
            },
            context,
        )

    assert failed.value.code == "EXECUTION_FAILED"
    diagnostic = next(
        record.getMessage()
        for record in caplog.records
        if record.name == "agent_bot.services.chat_tools"
    )
    assert "session_id=qq_safe_session" in diagnostic
    assert "workflow_key=image-z-image-int8" in diagnostic
    assert "call_index=1" in diagnostic
    assert "code=EXECUTION_FAILED" in diagnostic
    assert "PRIVATE_POSITIVE_PROMPT" not in diagnostic
    assert "PRIVATE_NEGATIVE_PROMPT" not in diagnostic
    assert "PRIVATE_COMFY_DETAIL" not in diagnostic


@pytest.mark.asyncio
async def test_local_workflow_validation_returns_field_details_without_values() -> None:
    registry = build_chat_tool_registry()
    context = ChatToolContext(
        session_id="private:10001",
        user_id="10001",
        allowed_tools=frozenset({"local_workflow_generate_image"}),
    )

    with pytest.raises(ChatToolError) as invalid:
        await registry.call(
            "local_workflow_generate_image",
            {"prompt": "private raw prompt"},
            context,
        )

    assert invalid.value.code == "INVALID_ARGUMENT"
    assert "positive_prompt" in str(invalid.value)
    assert "prompt" in str(invalid.value)
    assert "private raw prompt" not in str(invalid.value)
