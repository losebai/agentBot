from __future__ import annotations

import base64
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from workflow_fakes import TextWorkflowServiceStub

from agent_bot.integrations.dispatch_ai import (
    DispatchAiClient,
    DispatchAiImageError,
    DispatchAiProtocolError,
    DispatchAiRejectedError,
    DispatchAiUnavailableError,
)
from agent_bot.integrations.gpt_sovits import GeneratedSpeech
from agent_bot.integrations.onebot.tooling import ChatToolSessionLease
from agent_bot.model_guard import ModelGuardUnavailableError, ModelNotAvailableError
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import ChatContext, ChatMediaItem, ChatMediaResponse
from agent_bot.services.chat import ChatService
from agent_bot.services.chat.errors import ChatBusyError, ChatRateLimitedError
from agent_bot.services.chat_tools import (
    ChatToolContext,
    CollectedSpeech,
    SpeechCollector,
    build_chat_tool_registry,
)
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationJobExpiredError,
    ConversationQueueFullError,
    ConversationResult,
)
from agent_bot.services.replies import GeneratedReply, ReplyIntent
from agent_bot.services.workflow_execution import (
    GeneratedWorkflowImage,
    WorkflowImageCollector,
)
from agent_bot.state import ConversationStore


class RecordingComposer:
    """Expose an unintended second-model request while keeping delivery observable."""

    def __init__(self) -> None:
        """Initialize a list of the actual composition intents received."""

        self.intents: list[ReplyIntent] = []

    async def compose(self, intent: ReplyIntent) -> GeneratedReply:
        """Record the request and return text that would reveal an unwanted send."""

        self.intents.append(intent)
        return GeneratedReply(text="不应生成的二次解释", requestId=intent.request_id)


def onebot_mock() -> AsyncMock:
    """让 OneBot 便捷发送接口和原始媒体接口共用离线 API 替身。"""

    bot = AsyncMock()

    async def send_group_msg(**kwargs):
        """模拟真实 OneBot 群聊便捷 API 的转发。"""
        return await bot.call_api("send_group_msg", **kwargs)

    async def send_private_msg(**kwargs):
        """模拟真实 OneBot 私聊便捷 API 的转发。"""
        return await bot.call_api("send_private_msg", **kwargs)

    bot.send_group_msg.side_effect = send_group_msg
    bot.send_private_msg.side_effect = send_private_msg
    return bot


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    ChatBusyError("busy"), ChatRateLimitedError("limited"),
    ConversationQueueFullError("full"), ConversationJobExpiredError("expired"),
])
async def test_queue_overload_failures_are_silent(monkeypatch, chat_plugin, error) -> None:
    """过载与过期不能发送固定文案或再次请求模型。"""

    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    generate = AsyncMock(return_value="不应生成")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", generate)
    await chat_plugin._ConversationProcessor().deliver_failure(queued_job(), error)
    assert not bot.mock_calls
    generate.assert_not_awaited()


def queued_job(**overrides: object) -> ConversationJob:
    """Build a validated queue job with explicit overrides for each boundary test."""

    values: dict[str, object] = {
        "jobId": "job-agent-1",
        "requestId": "qq-job-agent-1",
        "createdAt": 1_800_000_000,
        "sessionId": "qq_session_agent",
        "botId": "90001",
        "scene": "group",
        "groupId": "20001",
        "userId": "10001",
        "prompt": "查询战绩后回答",
        "userName": "测试用户",
        "chatContext": ChatContext(
            scene="group",
            groupId="20001",
            senderId="10001",
            senderName="测试用户",
        ),
        "chatTools": ("naraka_query",),
    }
    values.update(overrides)
    return ConversationJob.model_validate(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_fails", [False, True])
async def test_processor_keeps_first_of_two_collected_images(
    monkeypatch, chat_plugin, agent_fails,
) -> None:
    """Both Agent outcomes must persist only the first already-validated image."""

    images = (
        GeneratedWorkflowImage(media_type="image/png", data=b"\x89PNG\r\n\x1a\nfirst"),
        GeneratedWorkflowImage(media_type="image/png", data=b"\x89PNG\r\n\x1a\nsecond"),
    )

    @asynccontextmanager
    async def open_session(job):
        """Represent the committed upstream collector's multi-image output contract."""

        yield ChatToolSessionLease(
            tools=job.chat_tools, token="workflow-token",
            collector=SimpleNamespace(images=images, queued_task_id=None),
        )

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", open_session)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", AsyncMock(
        return_value="模型完成回复",
        side_effect=DispatchAiUnavailableError("offline") if agent_fails else None,
    ))
    job = queued_job(chatTools=("local_workflow_generate_image",))

    result = await chat_plugin._ConversationProcessor().generate(job)

    assert result.kind == "WORKFLOW_IMAGES"
    assert len(result.workflow_images) == 1
    assert base64.b64decode(result.workflow_images[0].data) == images[0].data
    assert ConversationResult.model_validate_json(result.model_dump_json(by_alias=True)) == result


@pytest.mark.asyncio
async def test_processor_creates_ephemeral_tool_session_only_when_worker_runs(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify processor creates ephemeral tool session only when worker runs."""

    generate = AsyncMock(return_value="Agent 完成多步推理后的回答")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", generate)
    opened: list[str] = []

    @asynccontextmanager
    async def open_session(job):
        opened.append(job.job_id)
        yield job.chat_tools, "ephemeral-tool-token"

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", open_session)
    job = queued_job()

    result = await chat_plugin._ConversationProcessor().generate(job)

    assert opened == ["job-agent-1"]
    assert result.kind == "TOOL_REPLY"
    assert result.reply.text == "Agent 完成多步推理后的回答"
    assert result.delivery_parts == ("Agent 完成多步推理后的回答",)
    assert generate.await_args.kwargs["request_id"] == "qq-job-agent-1"
    assert generate.await_args.kwargs["chat_tools"] == ("naraka_query",)
    assert generate.await_args.kwargs["tool_session_token"] == "ephemeral-tool-token"
    assert "ephemeral-tool-token" not in job.model_dump_json(by_alias=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_speech", [False, True])
async def test_processor_returns_only_workflow_image_without_final_text(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    with_speech: bool,
) -> None:
    """图片完成后只发送图片，不再追加 Agent 收尾文字。"""

    generate = AsyncMock(return_value="图片已经生成。")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", generate)
    collector = WorkflowImageCollector(max_total_bytes=1024)
    await collector.reserve_call()
    await collector.add(
        GeneratedWorkflowImage(media_type="image/png", data=b"\x89PNG\r\n\x1a\none")
    )
    speech_collector = SpeechCollector()
    if with_speech:
        await speech_collector.restore("画完啦。", b"RIFF" + b"\x00" * 4 + b"WAVEfmt ")
    @asynccontextmanager
    async def open_session(job):
        yield ChatToolSessionLease(
            tools=job.chat_tools,
            token="workflow-token",
            collector=collector,
            speech_collector=speech_collector,
        )

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", open_session)
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    job = queued_job(
        prompt="生成一张图片",
        chatTools=("local_workflow_list", "local_workflow_generate_image"),
        imageStartReply=GeneratedReply(
            text="好啦，我开始画了。", requestId="qq-job-agent-1",
        ).model_dump(by_alias=True),
    )
    processor = chat_plugin._ConversationProcessor()

    result = await processor.generate(job)

    assert result.kind == "WORKFLOW_IMAGES"
    assert len(result.workflow_images) == 1
    assert result.reply is None
    assert await processor.delivery_count(job, result) == 1
    await processor.deliver_part(job, result, 0)
    bot.call_api.assert_awaited_once()
    message = bot.call_api.await_args.kwargs["message"]
    assert message.extract_plain_text() == ""
    assert len(message["image"]) == 1


@pytest.mark.parametrize("cloud", [False, True])
async def test_image_start_is_delivered_before_generation_and_not_repeated(
    monkeypatch, chat_plugin, cloud,
) -> None:
    """恢复同一任务时不能重复开画答复，本地工具与云端绘图都必须先答复。"""

    order = []
    bot = onebot_mock()
    bot.call_api.side_effect = lambda *_, **__: order.append("send") or {"message_id": 1}
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    start = AsyncMock(side_effect=lambda *_, **__: order.append("start") or "我来画，稍等。")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_image_start_queued", start)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_image_generation_enabled", True)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_local_workflow_enabled", True)
    job = queued_job(
        jobId=f"image-start-{cloud}", requestId=f"request-image-start-{cloud}",
        prompt="给这段故事配个插图", mediaOutput="IMAGE" if cloud else None,
        chatTools=() if cloud else ("local_workflow_generate_image",),
    )
    image = GeneratedWorkflowImage(media_type="image/png", data=b"\x89PNG\r\n\x1a\ncat")
    workflow = TextWorkflowServiceStub(generate=AsyncMock(
        side_effect=lambda **_: order.append("generate") or image,
    ))
    monkeypatch.setattr(chat_plugin, "workflow_service", workflow)
    enqueue = AsyncMock(side_effect=lambda _: order.append("enqueue") or "1-0")
    monkeypatch.setattr(chat_plugin.image_queue, "enqueue", enqueue)
    context = None

    @asynccontextmanager
    async def open_session(job):
        """每次创建新的收集器，避免内存状态掩盖重复答复。"""

        nonlocal context
        context = ChatToolContext(
            session_id=job.session_id, user_id=job.user_id,
            allowed_tools=frozenset(job.chat_tools), workflow_service=workflow,
            workflow_collector=WorkflowImageCollector(),
        )
        yield ChatToolSessionLease(
            tools=job.chat_tools, token="image-token", collector=context.workflow_collector,
        )

    async def agent(*_, **__):
        """让真实图片工具触发预告，然后返回不应发送的收尾文本。"""

        await build_chat_tool_registry().call(
            "local_workflow_generate_image",
            {"workflow_key": "portrait", "positive_prompt": "cat",
             "start_reply": "我来画，稍等。"}, context,
        )
        return "不要重复发送这个收尾"

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", open_session)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", agent)
    media = ChatMediaResponse(items=(ChatMediaItem(
        type="IMAGE", mimeType="image/png", data=base64.b64encode(image.data).decode(),
    ),))
    monkeypatch.setattr(chat_plugin.chat_service, "generate_media_queued", AsyncMock(
        side_effect=lambda *_, **__: order.append("generate") or media,
    ))
    result = await chat_plugin._ConversationProcessor().generate(job)
    start_order = ["start"] if cloud else []
    assert order == [*start_order, "send", "enqueue"]
    assert result.kind == "IMAGE_QUEUED"
    # 模拟生成结果落盘前重启；已发送预告从 Redis 恢复。
    result = await chat_plugin._ConversationProcessor().generate(job)
    assert order == [*start_order, "send", "enqueue", "enqueue"]
    assert start.await_count == (1 if cloud else 0)
    task = enqueue.await_args.args[0]
    assert task == enqueue.await_args_list[0].args[0]
    result = await chat_plugin._ConversationProcessor().generate(task)
    await chat_plugin._ConversationProcessor().deliver_part(task, result, 0)
    messages = [call.kwargs["message"] for call in bot.call_api.await_args_list]
    assert messages[0].extract_plain_text().strip() == "我来画，稍等。"
    assert [segment.type for segment in messages[1]] == ["image"]


@pytest.mark.parametrize("cloud", [False, True])
@pytest.mark.parametrize("failure", ["blank", "send"])
async def test_failed_image_start_never_submits_or_repeats_uncertain_send(
    monkeypatch, chat_plugin, cloud, failure,
) -> None:
    """预告为空或平台发送失败时不能提交绘图，恢复也不能重复不确定的发送。"""

    from agent_bot.services.chat_tools import ChatToolError
    from agent_bot.services.tool_operations import ToolOperationConflict

    bot = onebot_mock()
    if failure == "send":
        bot.call_api.side_effect = ConnectionError("platform response lost")
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    start = AsyncMock(return_value="  " if failure == "blank" else "我来画。")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_image_start_queued", start)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_image_generation_enabled", True)
    job = queued_job(
        jobId=f"failed-start-{cloud}-{failure}", prompt="画一只猫",
        mediaOutput="IMAGE" if cloud else None,
        chatTools=() if cloud else ("local_workflow_generate_image",),
    )
    workflow = TextWorkflowServiceStub(generate=AsyncMock())
    context = None

    @asynccontextmanager
    async def open_session(job):
        """恢复新进程的工具上下文，保留真实预告持久化端口。"""

        nonlocal context
        context = ChatToolContext(
            session_id=job.session_id, user_id=job.user_id,
            allowed_tools=frozenset(job.chat_tools), workflow_service=workflow,
            workflow_collector=WorkflowImageCollector(),
        )
        yield ChatToolSessionLease(
            tools=job.chat_tools, token="image-token", collector=context.workflow_collector,
        )

    async def agent(*_, **__):
        """通过真实图片工具执行，失败向上传播，不补发其他回复。"""

        await build_chat_tool_registry().call(
            "local_workflow_generate_image",
            {"workflow_key": "portrait", "positive_prompt": "cat",
             "start_reply": "  " if failure == "blank" else "我来画。"},
            context,
        )

    media = AsyncMock()
    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", open_session)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", agent)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_media_queued", media)
    for _ in range(2):
        with pytest.raises((
            ChatToolError, DispatchAiProtocolError, ConnectionError, ToolOperationConflict,
        )):
            await chat_plugin._ConversationProcessor().generate(job)
    workflow.generate.assert_not_awaited()
    media.assert_not_awaited()
    assert start.await_count == (1 if cloud else 0)
    assert bot.call_api.await_count == (1 if failure == "send" else 0)


@pytest.mark.parametrize("answer", ["今天聊什么？", "我不想生成自己的照片，换个风景吧。"])
async def test_unused_image_tool_does_not_send_start_notice(
    monkeypatch, chat_plugin, answer,
) -> None:
    """模型选择普通回答或拒绝时，只发送主正文，不生成预告或提交绘图。"""

    @asynccontextmanager
    async def open_session(job):
        """提供空图片收集器，模拟模型选择直接回答。"""

        yield ChatToolSessionLease(
            tools=job.chat_tools, token="image-token", collector=WorkflowImageCollector(),
        )

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", open_session)
    monkeypatch.setattr(
        chat_plugin.chat_service, "generate_queued", AsyncMock(return_value=answer),
    )
    start = AsyncMock()
    enqueue = AsyncMock()
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    monkeypatch.setattr(chat_plugin.image_queue, "enqueue", enqueue)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_image_start_queued", start)
    job = queued_job(chatTools=("local_workflow_generate_image",))
    processor = chat_plugin._ConversationProcessor()
    result = await processor.generate(job)
    assert result.kind == "TOOL_REPLY"
    assert result.reply.text == answer
    await processor.deliver_part(job, result, 0)
    bot.call_api.assert_awaited_once()
    assert bot.call_api.await_args.kwargs["message"].extract_plain_text() == answer
    enqueue.assert_not_awaited()
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_returns_text_when_tts_tool_is_available_but_not_called(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """开放 TTS 但模型未调用时必须保留正常文本回复。"""

    @asynccontextmanager
    async def open_session(job):
        """提供真实的空语音收集器，表示模型没有调用 TTS。"""

        yield ChatToolSessionLease(
            tools=job.chat_tools,
            token="chat-tool-token",
            speech_collector=SpeechCollector(),
        )

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", open_session)
    monkeypatch.setattr(
        chat_plugin.chat_service,
        "generate_queued",
        AsyncMock(return_value="这是普通文本回复。"),
    )
    job = queued_job(
        prompt="介绍一下你自己",
        chatTools=("naraka_query", "tts_generate"),
    )

    result = await chat_plugin._ConversationProcessor().generate(job)

    assert result.kind == "TOOL_REPLY"
    assert result.reply.text == "这是普通文本回复。"
    assert result.delivery_parts == ("这是普通文本回复。",)


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_fails", [False, True])
async def test_processor_persists_and_delivers_tts_tool_wav(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    agent_fails: bool,
) -> None:
    """MCP 已生成的 WAV 不受 Agent 收尾结果影响并作为 OneBot record 投递。"""

    wav = b"RIFF" + (16).to_bytes(4, "little") + b"WAVEfmt " + b"voice"
    audio_path = tmp_path / "voice.wav"
    audio_path.write_bytes(wav)
    speech = CollectedSpeech(
        text="你好，今天也要开心。",
        speech=GeneratedSpeech(
            path=audio_path,
            media_type="audio/wav",
            bytes=len(wav),
        ),
    )

    @asynccontextmanager
    async def open_session(job):
        """提供已经由真实 MCP 工具写入的请求级语音结果。"""

        yield SimpleNamespace(
            tools=job.chat_tools,
            token="tts-token",
            collector=None,
            speech_collector=SimpleNamespace(speech=speech),
        )

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", open_session)
    monkeypatch.setattr(
        chat_plugin.chat_service,
        "generate_queued",
        AsyncMock(
            return_value="语音已经生成。",
            side_effect=(
                DispatchAiUnavailableError("agent finalization failed")
                if agent_fails
                else None
            ),
        ),
    )
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    job = queued_job(prompt="请用语音打个招呼", chatTools=("tts_generate",))
    processor = chat_plugin._ConversationProcessor()

    result = await processor.generate(job)

    assert result.kind == "MEDIA"
    assert result.media.text == "你好，今天也要开心。"
    assert base64.b64decode(result.media.items[0].data) == wav
    await processor.deliver_part(job, result, 0)
    bot.call_api.assert_awaited_once_with(
        "send_group_msg",
        group_id=20001,
        message=Message(MessageSegment.record(f"base64://{result.media.items[0].data}")),
    )


@pytest.mark.asyncio
async def test_processor_sends_image_alone_then_mentions_sender_in_followup(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """群聊工作流图片必须独立发送，完成回复再原生 @ 请求发送人。"""

    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    job = queued_job(
        prompt="生成一张猫咪图片",
        chatTools=("local_workflow_list", "local_workflow_generate_image"),
        imageStartReply=GeneratedReply(
            text="好啦，我开始画了。", requestId="qq-job-agent-1",
        ).model_dump(by_alias=True),
    )
    result = ConversationResult(
        kind="MIXED",
        reply=dict(text="画好啦。", requestId="qq-job-agent-1"),
        deliveryParts=("画好啦。",),
        workflowImages=(
            {"mimeType": "image/png", "data": "iVBORw0KGgppbWFnZQ=="},
        ),
    )
    processor = chat_plugin._ConversationProcessor()

    await processor.deliver_part(job, result, 0)
    await processor.deliver_part(job, result, 1)

    messages = [call.kwargs["message"] for call in bot.call_api.await_args_list]
    assert messages[0] == Message(MessageSegment.image("base64://iVBORw0KGgppbWFnZQ=="))
    assert messages[1] == Message(
        [
            MessageSegment.at("10001"),
            MessageSegment.text(" 画好啦。"),
        ]
    )


@pytest.mark.asyncio
async def test_processor_sends_llm_image_start_as_plain_standalone_message(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """开画提示必须独立发送，不能提前 @ 用户或附带图片。"""

    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    job = queued_job(
        prompt="生成一张猫咪图片",
        chatTools=("local_workflow_list", "local_workflow_generate_image"),
        imageStartReply=GeneratedReply(
            text="好啦，我开始画了。", requestId="qq-job-agent-1",
        ).model_dump(by_alias=True),
    )

    await chat_plugin._ConversationProcessor().deliver_start_part(job, 0)

    bot.call_api.assert_awaited_once_with(
        "send_group_msg",
        group_id=20001,
        message=Message(MessageSegment.text("好啦，我开始画了。")),
    )


@pytest.mark.asyncio
async def test_processor_private_workflow_followup_has_no_at_segment(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """私聊工作流完成回复不能构造无效的群聊 @ 消息段。"""

    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    job = queued_job(
        scene="private",
        groupId=None,
        prompt="生成一张猫咪图片",
        chatTools=("local_workflow_list", "local_workflow_generate_image"),
        imageStartReply=GeneratedReply(
            text="好啦，我开始画了。", requestId="qq-job-agent-1",
        ).model_dump(by_alias=True),
    )
    result = ConversationResult(
        kind="MIXED",
        reply=dict(text="画好啦。", requestId="qq-job-agent-1"),
        deliveryParts=("画好啦。",),
        workflowImages=(
            {"mimeType": "image/png", "data": "iVBORw0KGgppbWFnZQ=="},
        ),
    )
    processor = chat_plugin._ConversationProcessor()

    await processor.deliver_part(job, result, 1)

    bot.call_api.assert_awaited_once_with(
        "send_private_msg",
        user_id=10001,
        message=Message(MessageSegment.text("画好啦。")),
    )


@pytest.mark.asyncio
async def test_processor_delivers_collected_workflow_image_when_agent_finishing_fails(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify processor delivers collected workflow image when agent finishing fails."""

    generate = AsyncMock(side_effect=DispatchAiUnavailableError("agent finishing failed"))
    explain = AsyncMock(return_value="不应再次调用模型")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", generate)
    monkeypatch.setattr(
        chat_plugin.chat_service,
        "generate_failure_explanation_queued",
        explain,
    )
    collector = WorkflowImageCollector(max_total_bytes=1024)
    await collector.reserve_call()
    await collector.add(
        GeneratedWorkflowImage(media_type="image/png", data=b"\x89PNG\r\n\x1a\nready")
    )

    @asynccontextmanager
    async def open_session(job):
        yield ChatToolSessionLease(
            tools=job.chat_tools,
            token="workflow-token",
            collector=collector,
        )

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", open_session)
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    job = queued_job(
        prompt="生成图片",
        chatTools=("local_workflow_list", "local_workflow_generate_image"),
    )
    processor = chat_plugin._ConversationProcessor()

    result = await processor.generate(job)

    assert result.kind == "WORKFLOW_IMAGES"
    assert result.reply is None
    assert len(result.workflow_images) == 1
    assert ConversationResult.model_validate_json(
        result.model_dump_json(by_alias=True)
    ) == result
    explain.assert_not_awaited()
    assert await processor.delivery_count(job, result) == 1
    await processor.deliver_part(job, result, 0)
    bot.call_api.assert_awaited_once()
    sent_message = bot.call_api.await_args.kwargs["message"]
    assert sent_message[0].type == "image"
    commit = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "commit_queued", commit)
    await processor.commit(job, result)
    commit.assert_awaited_once_with(
        "qq_session_agent",
        "生成图片",
        "[已生成图片]",
        chat_context=job.chat_context,
        request_id=job.request_id,
    )


@pytest.mark.asyncio
async def test_workflow_agent_rejection_is_silent_without_second_model(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """工作流主 Agent 拒绝后只记录脱敏诊断，不调用第二个模型或发送正文。"""

    caplog.set_level(
        logging.WARNING,
        logger="agent_bot.integrations.onebot.queue_processor",
    )
    generate = AsyncMock(side_effect=DispatchAiRejectedError("private upstream response"))
    composer = RecordingComposer()
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", generate)
    monkeypatch.setattr(chat_plugin, "_reply_composer", composer)

    @asynccontextmanager
    async def open_session(job):
        """Keep the real workflow-empty-collector failure branch offline."""

        yield ChatToolSessionLease(
            tools=job.chat_tools,
            token="workflow-token",
            collector=WorkflowImageCollector(),
        )

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", open_session)
    job = queued_job(
        prompt="生成图片",
        chatTools=("local_workflow_list", "local_workflow_generate_image"),
    )

    processor = chat_plugin._ConversationProcessor()
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    with pytest.raises(DispatchAiRejectedError) as caught:
        await processor.generate(job)
    await processor.deliver_failure(job, caught.value)
    assert composer.intents == []
    bot.call_api.assert_not_awaited()
    diagnostic = next(
        record.getMessage()
        for record in caplog.records
        if record.name == "agent_bot.integrations.onebot.queue_processor"
    )
    assert "stage=agent_generate" in diagnostic
    assert "job_id=job-agent-1" in diagnostic
    assert "request_id=qq-job-agent-1" in diagnostic
    assert "collected_images=0" in diagnostic
    assert "error=DispatchAiRejectedError" in diagnostic
    assert "private upstream response" not in diagnostic


@pytest.mark.asyncio
async def test_workflow_image_delivery_failure_logs_part_without_image_data(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """NapCat 图片投递失败时应记录任务和分段位置，但不记录图片内容。"""

    bot = onebot_mock()
    bot.call_api.side_effect = RuntimeError("PRIVATE_NAPCAT_RESPONSE")
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    caplog.set_level(
        logging.WARNING,
        logger="agent_bot.integrations.onebot.queue_processor",
    )
    job = queued_job(
        prompt="生成图片",
        chatTools=("local_workflow_list", "local_workflow_generate_image"),
    )
    result = ConversationResult(
        kind="WORKFLOW_IMAGES",
        workflowImages=(
            {
                "mimeType": "image/png",
                "data": "iVBORw0KGgpQUklWQVRFX0lNQUdFX0RBVEE=",
            },
        ),
    )

    with pytest.raises(RuntimeError, match="PRIVATE_NAPCAT_RESPONSE"):
        await chat_plugin._ConversationProcessor().deliver_part(job, result, 0)

    diagnostic = next(
        record.getMessage()
        for record in caplog.records
        if record.name == "agent_bot.integrations.onebot.queue_processor"
    )
    assert "stage=napcat_delivery" in diagnostic
    assert "job_id=job-agent-1" in diagnostic
    assert "request_id=qq-job-agent-1" in diagnostic
    assert "result_kind=WORKFLOW_IMAGES" in diagnostic
    assert "part_index=0" in diagnostic
    assert "image_count=1" in diagnostic
    assert "PRIVATE_NAPCAT_RESPONSE" not in diagnostic
    assert "iVBORw0KGgpQUklWQVRFX0lNQUdFX0RBVEE=" not in diagnostic


@pytest.mark.asyncio
async def test_processor_uses_vision_media_generation_for_queued_image(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    response = ChatMediaResponse(
        items=(ChatMediaItem(type="IMAGE", mimeType="image/png", data="iVBORw=="),)
    )
    generate = AsyncMock(return_value=response)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_media_queued", generate)
    monkeypatch.setattr(
        chat_plugin.chat_service, "generate_image_start_queued", AsyncMock(return_value="我来画。"),
    )
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: onebot_mock())
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "dispatch_chat_image_generation_enabled",
        True,
    )
    job = queued_job(
        jobId="job-image-1",
        requestId="qq-job-image-1",
        prompt="生成一张猫咪图片",
        chatTools=(),
        mediaOutput="IMAGE",
        backgroundImage=True,
    )

    result = await chat_plugin._ConversationProcessor().generate(job)

    assert result == ConversationResult(kind="MEDIA", media=response)
    assert generate.await_args.kwargs["output_type"] == "IMAGE"
    assert generate.await_args.kwargs["image_format"] == "png"
    assert generate.await_args.kwargs["request_id"] == "qq-job-image-1"
    assert generate.await_args.args[0] == "image:job-image-1"


@pytest.mark.asyncio
async def test_processor_delivers_cached_parts_through_target_bot(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify processor delivers cached parts through target bot."""

    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda bot_id: bot)
    job = queued_job(chatTools=(), mentionTargetId="10002")
    result = ConversationResult(
        kind="TEXT",
        reply=dict(text="第一段第二段", requestId="qq-job-agent-1"),
        deliveryParts=("第一段", "第二段"),
    )
    processor = chat_plugin._ConversationProcessor()

    await processor.deliver_part(job, result, 0)
    await processor.deliver_part(job, result, 1)

    assert bot.call_api.await_args_list[0].args == ("send_group_msg",)
    assert bot.call_api.await_args_list[0].kwargs == {
        "group_id": 20001,
        "message": Message(
            [
                MessageSegment.at("10002"),
                MessageSegment.text(" 第一段"),
            ]
        ),
    }
    assert bot.call_api.await_args_list[1].kwargs["message"] == Message(
        MessageSegment.text("第二段")
    )


@pytest.mark.asyncio
async def test_processor_commits_media_placeholder_only_after_delivery(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    commit = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "commit_queued", commit)
    job = queued_job(
        chatTools=(),
        mediaOutput="AUDIO",
        prompt="请用语音回答",
    )
    result = ConversationResult(
        kind="MEDIA",
        media=ChatMediaResponse(
            items=(ChatMediaItem(type="AUDIO", mimeType="audio/mpeg", data="SUQz"),)
        ),
    )

    await chat_plugin._ConversationProcessor().commit(job, result)

    commit.assert_awaited_once_with(
        "qq_session_agent",
        "请用语音回答",
        "[已生成语音]",
        chat_context=job.chat_context,
        request_id=job.request_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    DispatchAiImageError("sensitive"), ValueError("sensitive"),
])
async def test_processor_composes_sanitized_failure_once(monkeypatch, chat_plugin, error) -> None:
    """非模型自身的业务错误只提供脱敏事实，并投递一次模型生成的解释。"""

    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    generate = AsyncMock(return_value="模型生成的解释")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", generate)
    await chat_plugin._ConversationProcessor().deliver_failure(queued_job(), error)
    generate.assert_awaited_once()
    assert type(error).__name__ in generate.await_args.kwargs["instruction"]
    assert "sensitive" not in str(generate.await_args)
    bot.call_api.assert_awaited_once_with(
        "send_group_msg", group_id=20001,
        message=Message(MessageSegment.text("模型生成的解释")),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [
    DispatchAiUnavailableError("sensitive"),
    DispatchAiUnavailableError("sensitive", status_code=429),
    DispatchAiUnavailableError("sensitive", status_code=503),
    DispatchAiRejectedError("sensitive", status_code=403),
    DispatchAiProtocolError("sensitive"),
    ModelNotAvailableError("sensitive"),
    ModelGuardUnavailableError("sensitive"),
])
async def test_processor_model_failure_does_not_compose_or_send(
    monkeypatch, chat_plugin, error,
) -> None:
    """主模型不可用、拒绝或协议错误不能触发第二个模型或 OneBot 消息。"""

    composer = RecordingComposer()
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_reply_composer", composer)
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)

    await chat_plugin._ConversationProcessor().deliver_failure(queued_job(), error)

    assert composer.intents == []
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected_error", [
    ("timeout", DispatchAiUnavailableError),
    ("connection", DispatchAiUnavailableError),
    ("http_503", DispatchAiUnavailableError),
    ("http_403", DispatchAiRejectedError),
    ("rejected_result", DispatchAiRejectedError),
    ("invalid_json", DispatchAiProtocolError),
    ("empty", DispatchAiProtocolError),
    ("whitespace", DispatchAiProtocolError),
    ("empty_agent", DispatchAiProtocolError),
])
async def test_real_model_error_chain_remains_silent(
    monkeypatch, chat_plugin, failure, expected_error,
) -> None:
    """真实 HTTP 客户端及 ChatService 的异常传播链不能进入失败解释模型。"""

    requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        """Return a deterministic upstream failure without using external network."""

        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("private timeout", request=request)
        if failure == "connection":
            raise httpx.ConnectError("private network error", request=request)
        if failure.startswith("http_"):
            return httpx.Response(int(failure.removeprefix("http_")))
        if failure == "invalid_json":
            return httpx.Response(200, text="invalid response")
        if failure == "empty_agent":
            return httpx.Response(200, headers={"content-type": "text/event-stream"})
        return httpx.Response(200, json={
            "code": 400 if failure == "rejected_result" else 200,
            "data": {"replayContext": " \n " if failure == "whitespace" else ""},
        })

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai", timeout_seconds=1,
        transport=httpx.MockTransport(handle_request),
    )
    service = ChatService(
        client=client,
        store=ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60),
        system_prompt="测试系统提示词", max_input_chars=1000, cooldown_seconds=0,
        max_concurrent_requests=2,
        model_router=ContentModelRouter(
            text_model="chat", vision_model="vision", image_model="image", audio_model="audio",
        ),
        agent_enabled=failure == "empty_agent", web_search_enabled=False,
    )
    composer = RecordingComposer()
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "chat_service", service)
    monkeypatch.setattr(chat_plugin, "_reply_composer", composer)
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    processor = chat_plugin._ConversationProcessor()
    job = queued_job(chatTools=())

    try:
        with pytest.raises(expected_error) as caught:
            await processor.generate(job)
        await processor.deliver_failure(job, caught.value)
    finally:
        await client.aclose()

    assert len(requests) == 1
    assert composer.intents == []
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_composer_failure_is_silent(monkeypatch, chat_plugin) -> None:
    """解释生成器失败不得发送固定文字或再次尝试。"""

    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    generate = AsyncMock(side_effect=RuntimeError("secret"))
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", generate)
    await chat_plugin._ConversationProcessor().deliver_failure(queued_job(), ValueError("bad"))
    generate.assert_awaited_once()
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_processor_retry_reuses_provenance_without_resplitting(
    monkeypatch, chat_plugin,
) -> None:
    """投递失败及热更新均不得改变已持久化分段或重新调用模型。"""

    bot = onebot_mock()
    bot.call_api.side_effect = [ConnectionError("offline"), None]
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    generate = AsyncMock(return_value=" 模型回复 ")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", generate)
    processor = chat_plugin._ConversationProcessor()
    job = queued_job(chatTools=())
    result = await processor.generate(job)
    restored = ConversationResult.model_validate_json(result.model_dump_json(by_alias=True))
    assert restored.reply.request_id == job.request_id
    assert restored.reply.source == "LLM"
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_chunk_chars", 1)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_max_reply_chars", 1)
    with pytest.raises(ConnectionError):
        await processor.deliver_part(job, restored, 0)
    await processor.deliver_part(job, restored, 0)
    generate.assert_awaited_once()
    assert [str(call.kwargs["message"]) for call in bot.call_api.await_args_list] == [
        "模型回复", "模型回复",
    ]


@pytest.mark.asyncio
async def test_processor_truncation_never_adds_static_text(monkeypatch, chat_plugin) -> None:
    """长度限制只能裁剪模型文字，不得注入固定截断提示。"""

    generate = AsyncMock(return_value="abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", generate)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_max_reply_chars", 5)
    result = await chat_plugin._ConversationProcessor().generate(queued_job(chatTools=()))
    assert result.reply.text == "abcde"
    assert "".join(result.delivery_parts) == "abcde"


@pytest.mark.asyncio
@pytest.mark.parametrize("answer", ["", " \n "])
async def test_processor_rejects_blank_model_reply_without_second_model(
    monkeypatch, chat_plugin, answer,
) -> None:
    """来源适配器拒绝空白模型结果后仍须静默，不能把它当普通业务错误解释。"""

    composer = RecordingComposer()
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_reply_composer", composer)
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    monkeypatch.setattr(
        chat_plugin.chat_service, "generate_queued", AsyncMock(return_value=answer),
    )
    processor = chat_plugin._ConversationProcessor()
    job = queued_job(chatTools=())
    with pytest.raises((ValueError, DispatchAiProtocolError)) as caught:
        await processor.generate(job)
    await processor.deliver_failure(job, caught.value)

    assert composer.intents == []
    bot.call_api.assert_not_awaited()
    assert isinstance(caught.value, DispatchAiProtocolError)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_chars", [5, 3])
async def test_legacy_v2_start_notice_remains_one_exact_proven_part(
    monkeypatch, chat_plugin, chunk_chars: int,
) -> None:
    """Older v2 jobs without parts send the proven whole reply independently of hot config."""

    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_chunk_chars", chunk_chars)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_max_reply_chars", 5)
    job = queued_job(
        chatTools=("local_workflow_generate_image",),
        imageStartReply=GeneratedReply(
            text="abcdefghijklmnopqrstuvwxyz", requestId="qq-job-agent-1",
        ).model_dump(by_alias=True),
    )

    processor = chat_plugin._ConversationProcessor()
    assert await processor.start_delivery_count(job) == 1
    await processor.deliver_start_part(job, 0)

    assert [str(call.kwargs["message"]) for call in bot.call_api.await_args_list] == [
        "abcdefghijklmnopqrstuvwxyz",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_chars, expected", [(5, ["abcde"]), (3, ["abc", "de"])])
async def test_failure_explanation_truncation_contains_only_model_text(
    monkeypatch, chat_plugin, chunk_chars: int, expected: list[str],
) -> None:
    """失败解释只能发送受长度限制的模型原文，不能替换成固定标记。"""

    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_chunk_chars", chunk_chars)
    monkeypatch.setattr(chat_plugin.plugin_config, "dispatch_chat_max_reply_chars", 5)
    generate = AsyncMock(return_value="abcdefghijklmnopqrstuvwxyz")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", generate)

    await chat_plugin._ConversationProcessor().deliver_failure(queued_job(), ValueError("bad"))

    generate.assert_awaited_once()
    assert [str(call.kwargs["message"]) for call in bot.call_api.await_args_list] == expected
