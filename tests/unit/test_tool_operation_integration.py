"""真实 Redis 操作存储、工具注册表及队列媒体恢复的离线集成回归。"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from test_chat_tools import MentionBot, mention_composer, mention_gateway
from workflow_fakes import TextWorkflowServiceStub

from agent_bot.config import RuntimeConfig
from agent_bot.integrations.gpt_sovits import GeneratedSpeech
from agent_bot.integrations.mcp import ChatMcpServer
from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.integrations.onebot.tooling import ChatToolSessionManager
from agent_bot.integrations.tool_operations import RedisToolOperationStore
from agent_bot.services.chat_tools import (
    ChatToolContext,
    ChatToolError,
    SpeechCollector,
    build_chat_tool_registry,
)
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.services.request_deadline import request_deadline
from agent_bot.services.tool_operations import ImageEffect, SpeechEffect, ToolOperation
from agent_bot.services.tool_sessions import ToolSessionStore
from agent_bot.services.workflow_execution import GeneratedWorkflowImage, WorkflowImageCollector

_IMAGE_TOOL = "local_workflow_generate_image"
_IMAGE_ARGS = {
    "workflow_key": "portrait", "positive_prompt": "a small cat", "start_reply": "我来画。",
}
_TTS_ARGS = {"text": "你好，这是一条语音。"}
_MENTION_ARGS = {"target": "10086", "text": "请看群消息"}
_IMAGE_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6Tn8"
    "AAAAASUVORK5CYII="
)
_WAV_BYTES = b"RIFF\x04\x00\x00\x00WAVE"


class ToolHarness:
    """仅替换平台发送和模型生成端口，其余使用真实实现。"""

    def __init__(self, redis, tmp_path: Path) -> None:
        """构造共享持久记录与可观测的外部调用边界。"""
        self.redis = redis
        self.store = RedisToolOperationStore(redis, key_prefix="test:effects", ttl_seconds=60)
        self.deliveries: list[object] = []
        self.fail_send = False
        self.bot = MentionBot(self.call_api)
        self.registry = build_chat_tool_registry()
        self.workflow = TextWorkflowServiceStub(
            generate=AsyncMock(
                return_value=GeneratedWorkflowImage(media_type="image/png", data=_IMAGE_BYTES)
            )
        )
        self.speech_path = tmp_path / "generated.wav"
        self.speech_path.write_bytes(_WAV_BYTES)
        self.tts = SimpleNamespace(
            generate=AsyncMock(return_value=GeneratedSpeech(
                path=self.speech_path, media_type="audio/wav", bytes=len(_WAV_BYTES),
            ))
        )

    async def call_api(self, api: str, **data: object) -> object:
        """返回受控群成员并记录平台接收消息后响应丢失的场景。"""
        if api == "get_group_member_list":
            return [{"user_id": 10086, "nickname": "小明", "card": ""}]
        assert api == "send_group_msg"
        self.deliveries.append(data["message"])
        if self.fail_send:
            raise ConnectionError("response lost after platform accepted delivery")
        return {"message_id": 456}

    def context(self, *tools: str, job_id: str = "job-1") -> ChatToolContext:
        """每次调用创建新请求上下文和收集器，避免内存缓存掩盖重试问题。"""
        return ChatToolContext(
            session_id="qq_session", user_id="10001", group_id=12345,
            bot_user_id="90001", bot=self.bot, allowed_tools=frozenset(tools),
            workflow_service=self.workflow,
            workflow_collector=WorkflowImageCollector() if _IMAGE_TOOL in tools else None,
            tts_service=self.tts,
            tts_collector=SpeechCollector() if "tts_generate" in tools else None,
            job_id=job_id, operation_store=self.store,
        )

    def job(self, *tools: str) -> ConversationJob:
        """创建与工具记录相同任务编号的队列请求。"""
        return ConversationJob(
            jobId="job-1", requestId="request-1", createdAt=time.time(),
            sessionId="qq_session", botId="90001", scene="group", groupId="12345",
            userId="10001", prompt="请生成媒体", chatTools=tools,
        )


@pytest.fixture
async def harness(tmp_path: Path):
    """每个用例使用独立 Redis 替身和媒体目录。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        yield ToolHarness(redis, tmp_path)
    finally:
        await redis.aclose()


@pytest.mark.asyncio
async def test_mention_retry_in_new_context_restores_the_same_reply(harness) -> None:
    """Agent 失败后重建上下文不会再提醒群成员。"""
    first = await harness.registry.call(
        "mention_member", _MENTION_ARGS, harness.context("mention_member"),
    )
    retry = await harness.registry.call(
        "mention_member", _MENTION_ARGS, harness.context("mention_member"),
    )
    assert json.loads(first.content) == {"status": "queued", "target_user_id": 10086}
    assert retry == first
    assert len(harness.deliveries) == 0


@pytest.mark.asyncio
async def test_reply_selection_does_not_contact_platform(harness) -> None:
    """工具选择阶段不发送平台消息，平台故障仅由队列投递阶段处理。"""
    harness.fail_send = True
    context = harness.context("mention_member")
    await harness.registry.call("mention_member", _MENTION_ARGS, context)
    assert context.reply_collector.reply.text == _MENTION_ARGS["text"]
    assert harness.deliveries == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,arguments", [(_IMAGE_TOOL, _IMAGE_ARGS), ("tts_generate", _TTS_ARGS)],
)
async def test_generated_media_restores_in_new_context_without_external_generation(
    harness, tool: str, arguments: dict[str, str],
) -> None:
    """重试复用持久媒体；语音即使旧文件已删除也可恢复。"""
    first = await harness.registry.call(tool, arguments, harness.context(tool))
    harness.speech_path.unlink()
    retry_context = harness.context(tool)
    retry = await harness.registry.call(tool, arguments, retry_context)
    if tool == _IMAGE_TOOL:
        assert retry == first
        assert retry_context.workflow_collector.images == (
            GeneratedWorkflowImage(media_type="image/png", data=_IMAGE_BYTES),
        )
        assert harness.workflow.generate.await_count == 1
    else:
        assert json.loads(retry.content) == {"media_type": "audio/wav", "bytes": len(_WAV_BYTES)}
        assert retry_context.tts_collector.speech.audio == _WAV_BYTES
        assert retry_context.tts_collector.speech.text == _TTS_ARGS["text"]
        assert harness.tts.generate.await_count == 1


async def test_submitted_image_task_restores_without_enqueuing_or_generating_again(harness):
    """父请求恢复只取已入队任务编号，不重新提交图片或调用外部服务。"""

    context = harness.context(_IMAGE_TOOL)
    submit = AsyncMock(return_value="image-task-1")
    context.workflow_collector.submit = submit
    first = await harness.registry.call(_IMAGE_TOOL, _IMAGE_ARGS, context)
    retry = harness.context(_IMAGE_TOOL)
    retry.workflow_collector.submit = submit
    restored = await harness.registry.call(_IMAGE_TOOL, _IMAGE_ARGS, retry)
    assert json.loads(first.content) == {"status": "queued", "task_id": "image-task-1"}
    assert restored == first
    assert retry.workflow_collector.queued_task_id == "image-task-1"
    submit.assert_awaited_once()
    harness.workflow.generate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,changed", [
    ("mention_member", _MENTION_ARGS, {"target": "10086", "text": "另一条提醒"}),
    (_IMAGE_TOOL, _IMAGE_ARGS, {**_IMAGE_ARGS, "positive_prompt": "a dog"}),
    ("tts_generate", _TTS_ARGS, {"text": "另一条语音"}),
])
async def test_retry_cannot_change_completed_operation_signature(
    harness, tool: str, arguments: dict[str, str], changed: dict[str, str],
) -> None:
    """同任务改写副作用参数会被拒绝，不会绕过一次生成或提醒上限。"""
    await harness.registry.call(tool, arguments, harness.context(tool))
    with pytest.raises(ChatToolError) as conflict:
        await harness.registry.call(tool, changed, harness.context(tool))
    assert conflict.value.code == "EXECUTION_UNCERTAIN"
    assert len(harness.deliveries) + harness.workflow.generate.await_count + (
        harness.tts.generate.await_count
    ) == (0 if tool == "mention_member" else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", [_IMAGE_TOOL, "image_completion_reply"])
async def test_storage_rejects_result_kind_that_does_not_match_claimed_tool(harness, tool) -> None:
    """图片或完成通知不能被错误保存成语音，避免恢复时串用不同类型的结果。"""
    await harness.store.begin("job-1", tool, "a" * 64)
    speech = SpeechEffect(text="你好", data=base64.b64encode(_WAV_BYTES).decode("ascii"))
    with pytest.raises(ValueError, match="result type"):
        await harness.store.complete("job-1", tool, "a" * 64, speech)
    assert await harness.store.completed("job-1", tool) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments,invalid_data", [
    (_IMAGE_TOOL, _IMAGE_ARGS, "invalid-base64!"),
    ("tts_generate", _TTS_ARGS, base64.b64encode(b"not-a-wav-file").decode("ascii")),
])
async def test_invalid_persisted_media_is_not_collected_or_regenerated(
    harness, tool: str, arguments: dict[str, str], invalid_data: str,
) -> None:
    """损坏的持久载荷不能进入投递收集器，也不能触发重复生成。"""
    await harness.registry.call(tool, arguments, harness.context(tool))
    keys = await harness.redis.keys("test:effects:*")
    assert len(keys) == 1
    operation = ToolOperation.model_validate_json(await harness.redis.get(keys[0]))
    corrupted = operation.model_copy(update={
        "result": operation.result.model_copy(update={"data": invalid_data}),
    })
    await harness.redis.set(keys[0], corrupted.model_dump_json(), ex=60)
    context = harness.context(tool)
    with pytest.raises(ChatToolError):
        await harness.registry.call(tool, arguments, context)
    if tool == _IMAGE_TOOL:
        assert context.workflow_collector.images == ()
        assert harness.workflow.generate.await_count == 1
    else:
        assert context.tts_collector.speech is None
        assert harness.tts.generate.await_count == 1


@pytest.mark.asyncio
async def test_restored_media_enforces_decoded_size_cap(harness) -> None:
    """Base64 字符上限无法区分末尾填充，解码后仍严格拒绝超过 10 MiB 的图片。"""
    await harness.registry.call(_IMAGE_TOOL, _IMAGE_ARGS, harness.context(_IMAGE_TOOL))
    effect = ImageEffect(
        workflow_key="portrait", media_type="image/png",
        data=base64.b64encode(b"x" * 10_485_761).decode("ascii"),
    )
    keys = await harness.redis.keys("test:effects:*")
    record = ToolOperation.model_validate_json(await harness.redis.get(keys[0]))
    await harness.redis.set(
        keys[0], record.model_copy(update={"result": effect}).model_dump_json(), ex=60,
    )
    context = harness.context(_IMAGE_TOOL)
    with pytest.raises(ChatToolError) as error:
        await harness.registry.call(_IMAGE_TOOL, _IMAGE_ARGS, context)
    assert error.value.code == "LIMIT_EXCEEDED"
    assert context.workflow_collector.images == ()
    assert harness.workflow.generate.await_count == 1


def _session_manager(harness, monkeypatch):
    """使用真实令牌会话管理器，仅替换已连接机器人查找。"""
    config = RuntimeConfig(
        onebot_v11_access_token="test-token-that-is-longer-than-32-characters",
        chat_session_secret="test-secret-that-is-longer-than-32-characters",
        chat_mcp_enabled=True,
        chat_image_generation_enabled=True,
        chat_local_workflow_enabled=True,
        chat_tts_enabled=True,
    )
    sessions = ToolSessionStore(ttl_seconds=60, max_sessions=8)
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: sessions,
        naraka_service_provider=lambda: None, group_member_refresher=AsyncMock(return_value=[]),
        workflow_service_provider=lambda: harness.workflow,
        tts_service_provider=lambda: harness.tts,
        operation_store_provider=lambda: harness.store,
    )
    monkeypatch.setattr(manager, "queued_bot", lambda _bot_id: harness.bot)
    return config, sessions, manager


@pytest.mark.asyncio
async def test_queued_session_restores_mention_but_still_generates_final_text(
    harness, monkeypatch,
) -> None:
    """恢复已登记的提醒目标后仍生成最终正文，工具参数不能替代实际回答。"""
    await harness.registry.call(
        "mention_member", _MENTION_ARGS, harness.context("mention_member"),
    )
    config, _sessions, manager = _session_manager(harness, monkeypatch)

    @asynccontextmanager
    async def runtime_lease():
        """保持运行时生命周期而不访问外部服务。"""
        yield

    chat = SimpleNamespace(generate_queued=AsyncMock(return_value="最终回答"))
    processor = ConversationQueueProcessor(
        config_provider=lambda: config, chat_service_provider=lambda: chat,
        runtime_lease=runtime_lease, tool_session_provider=manager.open_queued_session,
        bot_provider=lambda _bot_id: harness.bot, reply_composer=mention_composer(),
        reply_gateway=mention_gateway(), operation_store_provider=lambda: harness.store,
        image_submitter=AsyncMock(), workflow_service_provider=lambda: harness.workflow,
    )
    job = harness.job("mention_member")
    result = await processor.generate(job)
    assert result.kind == "TOOL_REPLY"
    assert result.reply.text == "最终回答"
    assert await processor.delivery_count(job, result) == 1
    await processor.deliver_part(job, result, 0)
    assert len(harness.deliveries) == 1
    chat.generate_queued.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,arguments", [(_IMAGE_TOOL, _IMAGE_ARGS), ("tts_generate", _TTS_ARGS)],
)
async def test_queued_session_preloads_media_and_processor_recovers_agent_failure(
    harness, monkeypatch, tool: str, arguments: dict[str, str],
) -> None:
    """重建队列会话会先恢复媒体，Agent 再失败也无需重新生成或读取旧语音文件。"""
    await harness.registry.call(tool, arguments, harness.context(tool))
    harness.speech_path.unlink()
    config, sessions, manager = _session_manager(harness, monkeypatch)
    job = harness.job(tool)
    async with manager.open_queued_session(job) as lease:
        context = await sessions.resolve(lease.token)
        assert context.job_id == "job-1"
        if tool == _IMAGE_TOOL:
            assert lease.collector.images[0].data == _IMAGE_BYTES
        else:
            assert lease.speech_collector.speech.audio == _WAV_BYTES

    @asynccontextmanager
    async def runtime_lease():
        """模拟已获得运行时租约，不替换工具与媒体恢复逻辑。"""
        yield

    chat = SimpleNamespace(generate_queued=AsyncMock(side_effect=RuntimeError("agent failed")))
    processor = ConversationQueueProcessor(
        config_provider=lambda: config, chat_service_provider=lambda: chat,
        runtime_lease=runtime_lease, tool_session_provider=manager.open_queued_session,
        bot_provider=lambda _bot_id: harness.bot, reply_composer=mention_composer(),
        reply_gateway=mention_gateway(), operation_store_provider=lambda: harness.store,
        image_submitter=AsyncMock(), workflow_service_provider=lambda: harness.workflow,
    )
    result = await processor.generate(job)
    if tool == _IMAGE_TOOL:
        assert result.kind == "WORKFLOW_IMAGES"
        assert base64.b64decode(result.workflow_images[0].data) == _IMAGE_BYTES
        assert harness.workflow.generate.await_count == 1
    else:
        assert result.kind == "MEDIA"
        assert base64.b64decode(result.media.items[0].data) == _WAV_BYTES
        assert harness.tts.generate.await_count == 1


@pytest.mark.asyncio
async def test_parallel_queued_sessions_bind_only_their_own_image(harness, monkeypatch):
    """同一群会话的两个任务使用不同 scope，恢复时仍保持稳定。"""
    from agent_bot.services.workflow_inputs import workflow_input_scope

    _, sessions, manager = _session_manager(harness, monkeypatch)
    first = harness.job(_IMAGE_TOOL).model_copy(update={
        "job_id": "first", "image_urls": ("https://gchat.qpic.cn/first",),
    })
    second = first.model_copy(update={
        "job_id": "second", "image_urls": ("https://gchat.qpic.cn/second",),
    })
    async with manager.open_queued_session(first) as a, manager.open_queued_session(second) as b:
        first_context, second_context = await asyncio.gather(
            sessions.resolve(a.token), sessions.resolve(b.token),
        )
        assert first_context.image_urls == first.image_urls
        assert second_context.image_urls == second.image_urls
        assert first_context.workflow_request_scope == workflow_input_scope("first")
        assert second_context.workflow_request_scope == workflow_input_scope("second")
        assert first_context.workflow_request_scope != second_context.workflow_request_scope


@pytest.mark.asyncio
async def test_queued_prehydration_rejects_wrong_cached_operation_kind(
    harness, monkeypatch,
) -> None:
    """同时拥有图片和语音权限也不能接受图片键下损坏的语音结果。"""
    await harness.registry.call(_IMAGE_TOOL, _IMAGE_ARGS, harness.context(_IMAGE_TOOL))
    keys = await harness.redis.keys("test:effects:*")
    record = ToolOperation.model_validate_json(await harness.redis.get(keys[0]))
    corrupted = record.model_copy(update={"result": SpeechEffect(
        text="不应恢复", data=base64.b64encode(_WAV_BYTES).decode("ascii"),
    )})
    await harness.redis.set(keys[0], corrupted.model_dump_json(), ex=60)
    _, _, manager = _session_manager(harness, monkeypatch)
    with pytest.raises(ValueError, match="result type"):
        async with manager.open_queued_session(harness.job(_IMAGE_TOOL, "tts_generate")):
            pytest.fail("wrong operation kind was admitted to the queue tool session")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,arguments", [
    ("mention_member", _MENTION_ARGS), (_IMAGE_TOOL, _IMAGE_ARGS), ("tts_generate", _TTS_ARGS),
])
async def test_expired_context_refuses_all_effects(harness, tool, arguments) -> None:
    """直接调用注册表也必须拒绝过期上下文，不产生持久占位或外部副作用。"""
    context = harness.context(tool)
    context.deadline_at = time.monotonic() - 1
    with pytest.raises(ChatToolError) as error:
        await harness.registry.call(tool, arguments, context)
    assert error.value.code == "DEADLINE_EXCEEDED"
    assert not harness.deliveries
    assert harness.workflow.generate.await_count == harness.tts.generate.await_count == 0
    assert await harness.redis.dbsize() == 0


@pytest.mark.asyncio
async def test_mention_expiring_during_member_lookup_does_not_select_reply(harness) -> None:
    """成员查询期间用尽预算时不能继续登记回复。"""
    context = harness.context("mention_member")

    async def members():
        """返回有效成员，同时模拟预算耗尽。"""
        context.deadline_at = time.monotonic() - 1
        return [{"user_id": 10086, "nickname": "小明", "card": ""}]

    context.group_member_provider = members
    with pytest.raises(ChatToolError) as error:
        await harness.registry.call("mention_member", _MENTION_ARGS, context)
    assert error.value.code == "DEADLINE_EXCEEDED"
    assert context.reply_collector.reply is None
    assert harness.deliveries == []


@pytest.mark.asyncio
async def test_mcp_timeout_cancels_preparation_before_platform_effect(harness) -> None:
    """MCP 边界的总预算到期后取消成员查询，不能继续发送提醒。"""
    context = harness.context("mention_member")
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def members():
        """模拟正在等待外部成员目录，并记录取消确实传递。"""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    context.group_member_provider = members
    sessions = ToolSessionStore(ttl_seconds=60, max_sessions=8)
    server = ChatMcpServer(store=sessions, registry=harness.registry, allowed_hosts=["localhost"])
    async with sessions.open(context) as token:
        context.deadline_at = time.monotonic() + 0.05
        result = await server.call_tool(token, "mention_member", _MENTION_ARGS)
    assert started.is_set() and cancelled.is_set()
    assert result.is_error
    assert json.loads(result.content[0].text)["code"] == "DEADLINE_EXCEEDED"
    assert harness.deliveries == []
    assert await harness.redis.dbsize() == 0


@pytest.mark.asyncio
async def test_expired_queue_budget_cannot_open_tool_session(harness, monkeypatch) -> None:
    """任务剩余时限耗尽时不创建新工具会话或调用媒体服务。"""
    _, _, manager = _session_manager(harness, monkeypatch)
    with request_deadline(-1), pytest.raises(TimeoutError):
        async with manager.open_queued_session(harness.job(_IMAGE_TOOL)):
            pytest.fail("expired queue request opened a tool session")
    assert harness.workflow.generate.await_count == 0
