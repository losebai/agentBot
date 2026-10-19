"""队列只登记真实用户输入，派生图片任务、自动发言与定时结果不会冒充用户。"""

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationResult,
    PersistedGeneratedReply,
)


@asynccontextmanager
async def lease(*args):
    """替换运行时与工具租约，不连接平台或外部服务。"""
    yield ((), None)


@pytest.mark.parametrize("kind", ["text", "background_image", "ai_job", "auto_speak"])
async def test_queue_records_original_user_time_before_generation_and_skips_synthetic_jobs(kind):
    """模型失败仍保留原消息时间；后台派生任务只读取原上下文。"""
    receive = AsyncMock()
    finish = AsyncMock()
    chat = SimpleNamespace(generate_queued=AsyncMock(side_effect=RuntimeError("模型失败")),
                           commit_queued=AsyncMock(),
                           client=SimpleNamespace(acknowledge_job=AsyncMock()))
    processor = ConversationQueueProcessor(
        config_provider=lambda: SimpleNamespace(chat_chunk_chars=100),
        chat_service_provider=lambda: chat, runtime_lease=lease, tool_session_provider=lease,
        bot_provider=lambda _: None, reply_composer=None, reply_gateway=None,
        operation_store_provider=lambda: None, image_submitter=AsyncMock(),
        workflow_service_provider=lambda: None, receive_queued=receive, finish_received=finish,
    )
    values = dict(jobId="job", requestId="request", createdAt=10, sessionId="s",
                  botId="90001", scene="private", userId="10001", prompt="用户原问题",
                  historyGenerationId="a" * 32)
    if kind == "background_image":
        values.update(backgroundImage=True, mediaOutput="IMAGE")
        processor._generate_media = AsyncMock(side_effect=RuntimeError("模型失败"))
    elif kind == "ai_job":
        identity = "ai-job-" + "b" * 32
        values.update(jobId=identity, requestId=identity, aiJobId="b" * 32,
                      aiJobReply=PersistedGeneratedReply(text="定时结果", requestId=identity))
    elif kind == "auto_speak":
        values.update(autoSpeakMaxReplyChars=100)
    job = ConversationJob(**values)
    if kind == "ai_job":
        result = await processor.generate(job)
        await processor.commit(job, result)
        chat.commit_queued.assert_not_awaited()
        chat.client.acknowledge_job.assert_awaited_once_with("90001", "b" * 32)
    else:
        with pytest.raises(RuntimeError, match="模型失败"):
            await processor.generate(job)
    if kind == "text":
        receive.assert_awaited_once_with(
            "s", "用户原问题", request_id="request", created_at=datetime.fromtimestamp(10, UTC),
            chat_context=None, generation_id="a" * 32,
        )
    else:
        receive.assert_not_awaited()
    # 单次生成失败可能继续重试，直到显式终态通知才结束原输入的保护。
    finish.assert_not_awaited()
    processor._deliver_failure = AsyncMock(side_effect=RuntimeError("通知失败"))
    with pytest.raises(RuntimeError, match="通知失败"):
        await processor.deliver_failure(job, RuntimeError("最终失败"))
    if kind == "text":
        finish.assert_awaited_once_with("s", "request")
    else:
        finish.assert_not_awaited()


@pytest.mark.parametrize("silent,commit_error", [(False, False), (True, False), (False, True)])
async def test_commit_finishes_only_after_success_and_silent_reply_also_finishes(
    silent, commit_error,
):
    """成功和已完成工具静默回复结束保护；提交失败仍可能重试，不能提前解除。"""
    finish = AsyncMock()
    chat = SimpleNamespace(commit_queued=AsyncMock(
        side_effect=RuntimeError("提交失败") if commit_error else None,
    ))
    processor = ConversationQueueProcessor(
        config_provider=lambda: None, chat_service_provider=lambda: chat,
        runtime_lease=lease, tool_session_provider=lease, bot_provider=lambda _: None,
        reply_composer=None, reply_gateway=None, operation_store_provider=lambda: None,
        image_submitter=AsyncMock(), workflow_service_provider=lambda: None,
        finish_received=finish,
    )
    job = ConversationJob(jobId="job", requestId="request", createdAt=10, sessionId="s",
                          botId="90001", scene="private", userId="10001", prompt="用户问题")
    result = ConversationResult(kind="SILENT") if silent else ConversationResult(
        kind="TEXT", reply=PersistedGeneratedReply(text="回复", requestId="request"),
        deliveryParts=("回复",),
    )
    if commit_error:
        with pytest.raises(RuntimeError, match="提交失败"):
            await processor.commit(job, result)
        finish.assert_not_awaited()
    else:
        await processor.commit(job, result)
        finish.assert_awaited_once_with("s", "request")
