"""统一上下文接收入口保留失败输入，并在模型历史中排除本次输入。"""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_session_delivery import ContextStore

from agent_bot.model_router import ContentModelRouter
from agent_bot.services.chat import ChatRateLimitedError, ChatService
from agent_bot.session_delivery import SessionDeliveryStore
from agent_bot.state import ConversationStore


class ReceivedStore(ContextStore):
    """复用同步上下文替身，不触发与本测试无关的摘要压缩。"""

    def record_received(self, *args, **kwargs):
        """请求重试保留第一次登记的原时间。"""
        if not any(row[2]["request_id"] == kwargs["request_id"] for row in self.received):
            super().record_received(*args, **kwargs)
        return None


def make_service(*, cooldown_seconds=0):
    """只替换模型网络，读取和输入登记通过真实异步 facade。"""
    received = ReceivedStore()
    facade = SessionDeliveryStore(lambda: received)
    client = SimpleNamespace(complete=AsyncMock(side_effect=RuntimeError("模型失败")),
                             generate_media=AsyncMock(side_effect=RuntimeError("媒体失败")))
    service = ChatService(
        client=client, store=ConversationStore(history_turns=10, max_sessions=10, ttl_seconds=60),
        session_delivery=facade, system_prompt="角色", max_input_chars=1000,
        cooldown_seconds=cooldown_seconds, max_concurrent_requests=2,
        model_router=ContentModelRouter("chat", "vision", "image", "audio"),
    )
    return service, received, client


async def test_failed_direct_request_keeps_input_and_does_not_duplicate_current_prompt():
    """模型抛错之前已持久登记用户，当前输入仍只由提示词构造器追加一次。"""
    service, store, client = make_service()
    with pytest.raises(RuntimeError, match="模型失败"):
        await service.ask("s", "原始问题")
    assert len(store.received) == 1 and store.received[0][1] == "原始问题"
    assert store.finished_requests == [("s", store.received[0][2]["request_id"])]
    messages = client.complete.await_args.kwargs["messages"]
    assert [message.content for message in messages if message.role == "user"] == ["原始问题"]
    context = await service.conversation_context("s")
    assert [message.content for message in context.messages] == ["原始问题"]


async def test_queue_receive_preserves_timestamp_and_generation_without_second_registration():
    """排队生成只读接收阶段的输入，不用执行时间覆盖原时间或重新绑定代际。"""
    service, store, client = make_service()
    created = datetime(2026, 10, 19, 1, 2, tzinfo=UTC)
    await service.receive_queued("s", "排队问题", request_id="queue-r", created_at=created,
                                 generation_id="generation")
    with pytest.raises(RuntimeError, match="模型失败"):
        await service.generate_queued("s", "排队问题", request_id="queue-r")
    assert store.received == [("s", "排队问题", {
        "request_id": "queue-r", "created_at": created, "generation_id": "generation",
    })]
    assert store.finished_requests == []
    assert [message.content for message in client.complete.await_args.kwargs["messages"]
            if message.role == "user"] == ["排队问题"]


async def test_reply_composition_does_not_register_internal_instruction_as_user():
    """通知表达、开画提示和内部事实不是新的用户消息。"""
    service, store, client = make_service()
    client.complete.side_effect = None
    client.complete.return_value = "通知正文"
    await service.generate_reply_queued("s", "内部指令", request_id="notice",
                                        instruction="表达通知")
    assert store.received == []


async def test_direct_audio_failure_keeps_input_and_excludes_it_from_history():
    """音频请求也走统一输入入口，不因媒体模型失败丢失输入或重复拼接。"""
    service, store, client = make_service()
    with pytest.raises(RuntimeError, match="媒体失败"):
        await service.respond_media(
            "s", "请说一句话", AsyncMock(), output_type="AUDIO", timeout_seconds=30,
            max_bytes=100000, image_format=None, voice=None, audio_format=None, user="u",
        )
    assert len(store.received) == 1
    assert store.finished_requests == [("s", store.received[0][2]["request_id"])]
    messages = client.generate_media.await_args.kwargs["messages"]
    assert [message.content for message in messages if message.role == "user"] == ["请说一句话"]


async def test_admission_rejection_finishes_received_input_before_model_call():
    """准入失败发生在模型调用之前，仍须结束已经登记的用户输入保护。"""
    service, store, client = make_service(cooldown_seconds=60)
    service.accept_immediate("s", "已占用冷却")
    with pytest.raises(ChatRateLimitedError):
        await service.ask("s", "当前问题")
    assert len(store.finished_requests) == 1
    assert store.finished_requests[0][1] == store.received[0][2]["request_id"]
    client.complete.assert_not_awaited()


async def test_cancelled_direct_request_finishes_input_protection():
    """模型等待期间取消只终止请求，原用户输入不丢失且不会永久保持活动状态。"""
    service, store, client = make_service()
    started = asyncio.Event()

    async def wait_for_cancel(**kwargs):
        """用本地事件控制模型等待，避免连接平台或计时猜测。"""
        started.set()
        await asyncio.Event().wait()

    client.complete.side_effect = wait_for_cancel
    task = asyncio.create_task(service.ask("s", "取消的问题"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.received[0][1] == "取消的问题"
    assert store.finished_requests == [("s", store.received[0][2]["request_id"])]
