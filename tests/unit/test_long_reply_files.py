"""超长 QQ 回复以完整 UTF-8 文件投递，失败重试使用持久结果。"""

import base64
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_onebot_replies import runtime_config

from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.integrations.onebot.replies import OneBotReplyGateway
from agent_bot.integrations.sqlite_queue import SqliteConversationQueueStore
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationQueue,
    ConversationResult,
    QueuePolicy,
)
from agent_bot.services.replies import GeneratedReply


@pytest.mark.parametrize("scene", ["group", "private"])
async def test_file_gateway_preserves_complete_utf8_reply(scene):
    """文件不依赖 NapCat 与机器人共享目录，并发送到原会话。"""
    bot = SimpleNamespace(call_api=AsyncMock(return_value={"file_id": "file"}))
    reply = GeneratedReply.model_validate(
        {"text": 'value = """a\n\nb"""\n' * 3000, "requestId": "request/unsafe"},
        context={"file_reply": True},
    )
    assert "\n\n" in reply.text
    gateway = OneBotReplyGateway(lambda: runtime_config())
    await gateway.send_reply_file(bot, scene=scene, target_id="12345", reply=reply)
    args = bot.call_api.await_args
    assert args.args == (f"upload_{scene}_file",)
    assert args.kwargs[f"{'group' if scene == 'group' else 'user'}_id"] == 12345
    assert args.kwargs["name"].startswith("reply-")
    assert args.kwargs["name"].endswith(".txt")
    assert "/" not in args.kwargs["name"]
    assert base64.b64decode(args.kwargs["file"].removeprefix("base64://")).decode() == reply.text


@pytest.mark.parametrize("tools", [(), ("memory",)])
@pytest.mark.parametrize("scene", ["group", "private"])
async def test_long_queue_reply_is_file_and_survives_config_change(tools, scene):
    """超长普通及工具正文不裁剪，持久化后改变字数限制也不改变投递方式。"""
    config = runtime_config(chunk_chars=3, max_reply_chars=7)
    answer = 'value = """a\n\nb"""\n' * 10
    chat = SimpleNamespace(
        generate_queued=AsyncMock(return_value=answer), commit_queued=AsyncMock(),
    )
    bot = SimpleNamespace(
        call_api=AsyncMock(return_value={"file_id": "file"}),
        send_group_msg=AsyncMock(return_value={"message_id": 1}),
    )

    @asynccontextmanager
    async def lease():
        """模拟运行时租约，无生产连接。"""
        yield

    @asynccontextmanager
    async def session(job):
        """仅提供模型入参，不执行记忆工具。"""
        yield (job.chat_tools, "fake" if job.chat_tools else None)

    processor = ConversationQueueProcessor(
        config_provider=lambda: config, chat_service_provider=lambda: chat,
        runtime_lease=lease, tool_session_provider=session, bot_provider=lambda _: bot,
        reply_composer=SimpleNamespace(), reply_gateway=OneBotReplyGateway(lambda: config),
        operation_store_provider=lambda: None, image_submitter=AsyncMock(),
        workflow_service_provider=lambda: None,
    )
    job = ConversationJob(
        jobId="job", requestId="request", createdAt=1, sessionId="session", botId="90001",
        scene=scene, groupId="12345" if scene == "group" else None,
        userId="10001", prompt="写代码", chatTools=tools,
        mentionTargetId="10002" if scene == "group" else None,
    )
    result = await processor.generate(job)
    assert result.reply.text == answer
    assert getattr(result, "reply_as_file", False)
    result = ConversationResult.model_validate_json(result.model_dump_json(by_alias=True))
    config = runtime_config(chunk_chars=100, max_reply_chars=1000)
    count = await processor.delivery_count(job, result)
    assert count == (2 if scene == "group" else 1)
    for index in range(count):
        await processor.deliver_part(job, result, index)
    await processor.commit(job, result)
    assert base64.b64decode(
        bot.call_api.await_args.kwargs["file"].removeprefix("base64://"),
    ).decode() == answer
    assert chat.commit_queued.await_args.args[2] == answer
    chat.generate_queued.assert_awaited_once()
    if scene == "group":
        bot.send_group_msg.assert_awaited_once()
        message = bot.send_group_msg.await_args.kwargs["message"]
        assert message[0].type == "at" and message[0].data["qq"] == "10002"


@pytest.mark.parametrize("failure", ["upload", "mention"])
async def test_file_upload_retry_reuses_sqlite_result_without_regeneration(tmp_path, failure):
    """上传或群提醒失败复用 SQLite 正文，已确认上传不能在提醒重试时再次发送。"""
    config = runtime_config(chunk_chars=3, max_reply_chars=7)
    chat = SimpleNamespace(
        generate_queued=AsyncMock(return_value="完整内容" * 100), commit_queued=AsyncMock(),
    )
    bot = SimpleNamespace(
        call_api=AsyncMock(side_effect=[
            ConnectionError("upload unavailable"), {"file_id": "file"},
        ] if failure == "upload" else None, return_value={"file_id": "file"}),
        send_group_msg=AsyncMock(side_effect=[
            ConnectionError("mention unavailable"), {"message_id": 1},
        ]),
    )

    @asynccontextmanager
    async def lease():
        """模拟没有外部资源的运行时。"""
        yield

    @asynccontextmanager
    async def session(_job):
        """纯文本不申请工具令牌。"""
        yield ((), None)

    processor = ConversationQueueProcessor(
        config_provider=lambda: config, chat_service_provider=lambda: chat,
        runtime_lease=lease, tool_session_provider=session, bot_provider=lambda _: bot,
        reply_composer=SimpleNamespace(), reply_gateway=OneBotReplyGateway(lambda: config),
        operation_store_provider=lambda: None, image_submitter=AsyncMock(),
        workflow_service_provider=lambda: None,
    )
    store = SqliteConversationQueueStore(
        str(tmp_path / "queue.sqlite3"), key_prefix="files", max_pending=2,
        result_ttl_seconds=60,
    )
    await store.initialize()
    job = ConversationJob(
        jobId="job", requestId="request", createdAt=1, sessionId="session", botId="90001",
        scene="group" if failure == "mention" else "private",
        groupId="12345" if failure == "mention" else None,
        mentionTargetId="10002" if failure == "mention" else None,
        userId="10001", prompt="写代码",
    )
    await store.enqueue(job)
    queue = ConversationQueue(
        store=store, processor=processor, policy=QueuePolicy(1, 2, 0.001, 0.001, 600),
        clock=lambda: 1,
    )
    try:
        entry = (await store.read_new(count=1, block_ms=1))[0]
        await queue.process_entry(entry)
        chat.generate_queued.assert_awaited_once()
        chat.commit_queued.assert_awaited_once()
        if failure == "upload":
            assert bot.call_api.await_count == 2
            assert bot.call_api.await_args_list[0] == bot.call_api.await_args_list[1]
        else:
            bot.call_api.assert_awaited_once()
            assert bot.send_group_msg.await_count == 2
    finally:
        await queue.stop()
