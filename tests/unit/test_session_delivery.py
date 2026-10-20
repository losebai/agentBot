"""统一会话发送 facade 只协调同步仓库与已确认回执，不执行平台发送。"""

import asyncio
import threading
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent_bot.models import ChatMessage
from agent_bot.services.tool_operations import DeliveryEffect
from agent_bot.session_delivery import SessionDeliveryStore
from agent_bot.state import ConversationContext


async def test_adoption_is_recorded_only_after_confirmed_delivery():
    """未知投递不加热，确认正文才交给真实召回回执进行幂等核对。"""
    delivery = SimpleNamespace(content="正文【记忆 #1 v2】", request_id="req")
    store = SimpleNamespace(finish_delivery=Mock(return_value=delivery))
    memory = SimpleNamespace(record_adoption=Mock())
    facade = SessionDeliveryStore(lambda: store, memory_store_provider=lambda: memory)
    assert await facade.finish_delivery("delivery", "42", session_id="s") is delivery
    memory.record_adoption.assert_called_once_with("s", "req", delivery.content)
    store.finish_delivery.side_effect = RuntimeError("未知平台结果")
    with pytest.raises(RuntimeError):
        await facade.finish_delivery("failed", None, session_id="s")
    assert memory.record_adoption.call_count == 1


class ContextStore:
    """在同步边界保存占位与已发送事件，记录执行线程以验证异步隔离。"""

    def __init__(self):
        """隔离每个测试的代际、回执与消息。"""
        self.pending = ["pending", "unknown"]
        self.finished = []
        self.received = []
        self.threads = []
        self.messages = []
        self.finished_requests = []

    def context(self, session_id, *, exclude_request_id=None):
        """读取同一消息流并按请求身份排除当前用户消息。"""
        self.threads.append(threading.get_ident())
        return ConversationContext(
            memory="",
            generation_id="generation",
            messages=tuple(
                message
                for message in self.messages
                if not (message.role == "user" and message.history_request_id == exclude_request_id)
            ),
        )

    def pending_deliveries(self, session_id, generation_id):
        """只提供等待确认的身份，不向 facade 泄露正文。"""
        return tuple(self.pending)

    def claim_delivery(self, **values):
        """模拟同步仓库通过唯一身份完成发送仲裁。"""
        self.threads.append(threading.get_ident())
        return SimpleNamespace(**values)

    def finish_delivery(self, session_id, delivery_id, message_id):
        """记录确认，不从未知平台结果伪造发送消息。"""
        self.threads.append(threading.get_ident())
        self.finished.append((session_id, delivery_id, message_id))
        self.pending.remove(delivery_id)
        self.messages.append(ChatMessage(role="assistant", content="已确认通知"))
        return SimpleNamespace(delivery_id=delivery_id, message_id=message_id)

    def record_received(self, session_id, content, **values):
        """保留输入的原始事件时间和代际，不在 facade 重写。"""
        self.threads.append(threading.get_ident())
        self.received.append((session_id, content, values))
        self.messages.append(
            ChatMessage(role="user", content=content, history_request_id=values["request_id"])
        )
        return "compression"

    def list_messages(self, session_id, generation_id, limit=100):
        """仅返回已确认投递快照。"""
        return tuple(self.finished)[-limit:]

    def record(self, **values):
        """导入已确认消息遵循原仓库契约。"""
        self.threads.append(threading.get_ident())
        return values["content"] == "已发送"

    def finish_request(self, session_id, request_id):
        """结束原用户请求，不伪造助手正文。"""
        self.threads.append(threading.get_ident())
        self.finished_requests.append((session_id, request_id))
        return None


async def test_facade_dispatches_to_current_store_and_preserves_receive_identity():
    """运行时替换后使用新仓库，接收时间和代际原样传递，写入离开事件循环。"""
    stores = [ContextStore()]
    facade = SessionDeliveryStore(lambda: stores[0])
    old = stores[0]
    stores[0] = ContextStore()
    created = datetime(2026, 10, 19, tzinfo=UTC)
    result = await facade.record_received(
        "s", "用户输入", request_id="request", created_at=created, generation_id="generation"
    )
    assert result == "compression"
    assert old.received == []
    assert stores[0].received == [
        (
            "s",
            "用户输入",
            {
                "request_id": "request",
                "created_at": created,
                "generation_id": "generation",
            },
        )
    ]
    assert all(thread != threading.get_ident() for thread in stores[0].threads)


async def test_finishing_received_request_uses_same_store_without_assistant_event():
    """失败终态只释放输入的压缩保护，不新增助手消息或成功发送回执。"""
    store = ContextStore()
    facade = SessionDeliveryStore(lambda: store)
    await facade.finish_received("s", "request")
    assert store.finished_requests == [("s", "request")]
    assert store.messages == [] and store.finished == []
    assert store.threads[-1] != threading.get_ident()


async def test_context_reconciles_confirmed_receipt_before_reading_same_stream():
    """重启后的队列回执只补记确认，未知占位不自动重发；当前输入不重复。"""
    store = ContextStore()
    store.record_received("s", "当前输入", request_id="current")
    receipts = SimpleNamespace(
        completed=AsyncMock(
            side_effect=[
                DeliveryEffect(signature="a" * 64, message_id="platform-1"),
                None,
            ]
        )
    )
    facade = SessionDeliveryStore(lambda: store, receipts_provider=lambda: receipts)
    context = await facade.context("s", exclude_request_id="current")
    assert [message.content for message in context.messages] == ["已确认通知"]
    assert store.finished == [("s", "pending", "platform-1")]
    assert store.pending == ["unknown"]
    assert receipts.completed.await_args_list[0].args == ("pending", "outgoing_message")


async def test_live_receipts_are_consumed_only_after_successful_finish():
    """仅平台明确确认的内存回执能恢复上下文，完成后释放内存占用。"""
    store = ContextStore()
    effect = DeliveryEffect(signature="a" * 64, message_id="live-1")
    live = {"pending": effect}
    facade = SessionDeliveryStore(lambda: store, live_receipts_provider=lambda: live)
    rows = await facade.list_messages("s", "generation")
    assert rows == (("s", "pending", "live-1"),)
    assert live == {}
    assert store.pending == ["unknown"]


async def test_claim_finish_and_record_keep_session_scope_and_legacy_arguments():
    """投递确认显式携带 session，避免跨会话查找投递身份；其他参数保持兼容。"""
    store = ContextStore()
    facade = SessionDeliveryStore(lambda: store)
    values = dict(
        session_id="s",
        generation_id="generation",
        delivery_id="pending",
        request_id="r",
        message_id=None,
        content="已发送",
        material_ids=("asset",),
    )
    claimed = await facade.claim_delivery(**values)
    assert claimed.content == "已发送" and claimed.material_ids == ("asset",)
    finished = await facade.finish_delivery("pending", "platform-2", session_id="s")
    assert finished.message_id == "platform-2"
    assert store.finished == [("s", "pending", "platform-2")]
    assert await facade.record(**values) is True


async def test_cancelled_write_waits_for_transaction_to_finish():
    """取消调用者也要等已开始事务结束，避免旧写入越过运行时释放边界。"""
    started = threading.Event()
    release = threading.Event()
    written = []

    def record(**values):
        """通过事件控制提交时机，不连接真实数据库。"""
        started.set()
        release.wait(timeout=2)
        written.append(values["content"])
        return True

    facade = SessionDeliveryStore(lambda: SimpleNamespace(record=record))
    task = asyncio.create_task(
        facade.record(
            session_id="s",
            generation_id="generation",
            delivery_id="d",
            request_id="r",
            message_id="m",
            content="已发送",
            material_ids=(),
        )
    )
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert written == ["已发送"]


async def test_cancelled_context_read_waits_for_worker_before_releasing_caller():
    """读取取消也须等待同步事务退出，运行时租约不能提前关闭其数据库池。"""
    started = threading.Event()
    release = threading.Event()
    exited = threading.Event()

    def context(session_id, *, exclude_request_id=None):
        """模拟取消不能中断的同步读取，使用事件确定工作线程结束边界。"""
        started.set()
        release.wait(timeout=2)
        exited.set()
        return ConversationContext(memory="", messages=())

    facade = SessionDeliveryStore(lambda: SimpleNamespace(context=context))
    task = asyncio.create_task(facade.context("s"))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    await asyncio.sleep(0)
    cancelled_before_exit = task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not cancelled_before_exit
    assert exited.is_set()
