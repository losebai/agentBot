"""只创建新上下文表，离线贯通接收、分段发送与下一次模型读取。"""

import json
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent_bot.integrations.onebot.outgoing import DeliveryScope, OutgoingRecorder
from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.integrations.onebot.replies import OneBotReplyGateway
from agent_bot.model_router import ContentModelRouter
from agent_bot.services.chat import ChatService
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationResult,
    PersistedGeneratedReply,
)
from agent_bot.services.replies import GeneratedReply
from agent_bot.session_context import SessionContextPolicy, SessionContextStore
from agent_bot.session_delivery import SessionDeliveryStore
from agent_bot.settings import DatabaseParams


@pytest.fixture
def context_store(tmp_path):
    """在同步夹具中建独立数据库，只执行新库初始化脚本。"""
    path = tmp_path / "unified.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text("utf-8"))
    store = SessionContextStore(
        DatabaseParams(backend="sqlite", sqlite_path=str(path)),
        policy=SessionContextPolicy(history_turns=10, max_history_chars=10000,
                                    memory_enabled=False, max_memory_chars=1000),
    )
    return path, store


async def test_unified_flow_uses_only_session_table_and_never_adds_combined_duplicate(
    context_store,
):
    """已发送分段和原始输入共用消息流，提交轮次不会再复制一份完整回复。"""
    path, store = context_store
    facade = SessionDeliveryStore(lambda: store)
    client = SimpleNamespace(complete=AsyncMock(return_value="abcdef"))
    service = ChatService(
        client=client, store=store, session_delivery=facade, system_prompt="角色",
        max_input_chars=1000, cooldown_seconds=0, max_concurrent_requests=2,
        model_router=ContentModelRouter("chat", "vision", "image", "audio"),
    )
    generation = await service.history_generation("s")

    async def scope(request_id):
        """显式提供可信的会话身份，不从模型正文推导。"""
        return DeliveryScope("s", generation, request_id)

    recorder = OutgoingRecorder(service_provider=lambda: service, store_provider=lambda: facade,
                                materials_provider=lambda: None, scope_provider=scope)
    gateway = OneBotReplyGateway(
        lambda: SimpleNamespace(chat_chunk_chars=3, chat_max_reply_chars=100), recorder=recorder,
    )
    bot = SimpleNamespace(send_private_msg=AsyncMock(side_effect=[
        {"message_id": 11}, {"message_id": 12},
    ]))
    await service.receive_queued(
        "s", "原问题", request_id="r", created_at=datetime(2026, 10, 19, tzinfo=UTC),
        generation_id=generation,
    )
    answer = await service.generate_queued("s", "原问题", request_id="r")
    current_messages = client.complete.await_args.kwargs["messages"]
    assert [row.content for row in current_messages if row.role == "user"] == ["原问题"]
    await gateway.send_bot(bot, scene="private", target_id="10001",
                           reply=GeneratedReply(text=answer, requestId="r"))
    await service.commit_queued("s", "原问题", answer, request_id="r")
    context = await service.conversation_context("s")
    assert [(row.role, row.content) for row in context.messages] == [
        ("user", "原问题"), ("assistant", "abc"), ("assistant", "def"),
    ]
    assert bot.send_private_msg.await_count == 2
    with sqlite3.connect(path) as connection:
        names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        assert {"chat_memory_session", "chat_memory_turn", "chat_memory_receipt",
                "chat_sent_message"}.isdisjoint(names)
        assert connection.execute("SELECT COUNT(*) FROM chat_session_context").fetchone()[0] == 1


async def test_inflight_question_stays_out_of_memory_and_failed_input_can_compress(context_store):
    """零历史窗口也不能提前摘要当前输入，失败结束后则解除保护并保存无助手的记忆。"""
    _, store = context_store
    store.update_policy(SessionContextPolicy(history_turns=0, max_history_chars=10000,
                                             memory_enabled=True, max_memory_chars=1000))
    facade = SessionDeliveryStore(lambda: store)

    async def fail_model(**kwargs):
        """读取真实构造的上下文，模型网络只以异常替身模拟。"""
        assert sum(message.content.count("CURRENT_MARKER")
                   for message in kwargs["messages"]) == 1
        context = await facade.context("s", exclude_request_id=kwargs["request_id"])
        assert "CURRENT_MARKER" not in context.memory
        raise RuntimeError("模型失败")

    client = SimpleNamespace(complete=AsyncMock(side_effect=fail_model))
    service = ChatService(
        client=client, store=store, session_delivery=facade, system_prompt="角色",
        max_input_chars=1000, cooldown_seconds=0, max_concurrent_requests=2,
        model_router=ContentModelRouter("chat", "vision", "image", "audio"),
    )
    # 本测试只验证同步摘要及压缩任务交付，后台模型压缩有独立生命周期测试。
    compression_queue = SimpleNamespace(submit=Mock())
    service._compression_queue = compression_queue
    with pytest.raises(RuntimeError, match="模型失败"):
        await service.ask("s", "CURRENT_MARKER")
    context = await facade.context("s")
    assert "CURRENT_MARKER" in context.memory
    assert context.messages == ()
    snapshot = compression_queue.submit.call_args.args[0]
    assert len(snapshot.turns) == 1
    assert snapshot.turns[0].user == "CURRENT_MARKER" and snapshot.turns[0].assistant == ""


async def test_cached_result_registers_original_receive_time_before_platform_delivery(
    context_store,
):
    """升级恢复的缓存结果不重新生成，也必须先按原时间登记用户，再确认助手投递。"""
    path, store = context_store
    facade = SessionDeliveryStore(lambda: store)
    client = SimpleNamespace(complete=AsyncMock())
    service = ChatService(
        client=client, store=store, session_delivery=facade, system_prompt="角色",
        max_input_chars=1000, cooldown_seconds=0, max_concurrent_requests=2,
        model_router=ContentModelRouter("chat", "vision", "image", "audio"),
    )
    generation = await service.history_generation("s")

    async def scope(request_id):
        """离线绑定当前用户会话，所有网络调用都由 AsyncMock 代替。"""
        return DeliveryScope("s", generation, request_id)

    @asynccontextmanager
    async def lease(*args):
        """缓存结果恢复保持生产运行时租约边界。"""
        yield

    config = SimpleNamespace(chat_chunk_chars=100, chat_max_reply_chars=100,
                             chat_history_max_chars=10000)
    recorder = OutgoingRecorder(service_provider=lambda: service, store_provider=lambda: facade,
                                materials_provider=lambda: None, scope_provider=scope)
    gateway = OneBotReplyGateway(lambda: config, recorder=recorder)
    bot = SimpleNamespace(send_private_msg=AsyncMock(return_value={"message_id": 11}))
    processor = ConversationQueueProcessor(
        config_provider=lambda: config, chat_service_provider=lambda: service,
        runtime_lease=lease, tool_session_provider=lease, bot_provider=lambda _: bot,
        reply_composer=None, reply_gateway=gateway, operation_store_provider=lambda: None,
        image_submitter=AsyncMock(), workflow_service_provider=lambda: None,
        outgoing_recorder=recorder, receive_queued=service.receive_queued,
        finish_received=service.finish_received,
    )
    job = ConversationJob(jobId="cached", requestId="cached-r", createdAt=1000,
                          sessionId="s", botId="90001", scene="private", userId="10001",
                          prompt="原始问题", historyGenerationId=generation)
    result = ConversationResult(
        kind="TEXT", reply=PersistedGeneratedReply(text="缓存回复", requestId="cached-r"),
        deliveryParts=("缓存回复",),
    )
    assert await processor.delivery_count(job, result) == 1
    await processor.deliver_part(job, result, 0)
    await processor.commit(job, result)
    context = await facade.context("s")
    assert [(message.role, message.content) for message in context.messages] == [
        ("user", "原始问题"), ("assistant", "缓存回复"),
    ]
    client.complete.assert_not_awaited()
    with sqlite3.connect(path) as connection:
        messages = json.loads(connection.execute(
            "SELECT messages_json FROM chat_session_context WHERE session_id='s'",
        ).fetchone()[0])
    assert messages[0]["created_at"] == "1970-01-01T00:16:40.000000"
