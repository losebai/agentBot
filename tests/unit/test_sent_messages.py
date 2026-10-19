"""使用独立 SQLite 验证投递回执、会话代际隔离及实际消息合并。"""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from agent_bot.database import database_connector
from agent_bot.memory_store import ConversationMemoryPolicy, MySqlConversationMemoryStore
from agent_bot.models import ChatMessage
from agent_bot.settings import DatabaseParams
from agent_bot.state import ConversationContext, ConversationStore


@pytest.fixture
def stores(tmp_path):
    """直接执行当前初始化脚本，避免读取安装包或连接部署数据库。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "sent.db"))
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text("utf-8"))
        connection.executescript(Path("sql/legacy/session-context-v1.sqlite.sql").read_text("utf-8"))
    now = [datetime(2026, 10, 19, tzinfo=UTC)]
    memory = MySqlConversationMemoryStore(
        params,
        policy=ConversationMemoryPolicy(
            history_turns=10, max_history_chars=10000, ttl_seconds=60,
            memory_enabled=False, max_memory_chars=1000,
        ),
        clock=lambda: now[0],
    )
    from agent_bot.sent_messages import SentMessageStore

    sent = SentMessageStore(params, connect=database_connector(params), clock=lambda: now[0])
    return memory, sent, now


def test_memory_generation_survives_empty_history_and_changes_on_reset_and_expiry():
    """没有普通轮次时也必须固定代际，否则主动消息会丢失会话归属。"""
    now = [0.0]
    store = ConversationStore(history_turns=0, max_sessions=2, ttl_seconds=60,
                              clock=lambda: now[0])
    first = store.ensure_generation("s")
    assert store.context("s").generation_id == first
    assert store.ensure_generation("s") == first
    assert store.clear("s")
    second = store.ensure_generation("s")
    assert second != first
    now[0] = 60.0
    assert store.ensure_generation("s") != second


async def test_record_is_idempotent_and_reset_rejects_old_epoch(stores):
    """重复投递只出现一次，重置后旧后台结果不能进入当前上下文。"""
    memory, sent, now = stores
    generation = memory.ensure_generation("s")
    assert memory.context("s").generation_id == generation
    values = dict(session_id="s", generation_id=generation, delivery_id="a" * 64,
                  request_id="request", message_id="qq-1", content="已发送文件 material-1",
                  material_ids=("material-1",))
    assert await sent.record(**values)
    assert not await sent.record(**values)
    rows = await sent.list_messages("s", generation)
    assert [(row.delivery_id, row.content, row.material_ids) for row in rows] == [
        ("a" * 64, "已发送文件 material-1", ("material-1",))
    ]
    memory.clear("s")
    new_generation = memory.ensure_generation("s")
    assert new_generation != generation
    assert not await sent.record(**(values | {"delivery_id": "b" * 64}))
    assert await sent.list_messages("s", generation) == ()
    assert await sent.list_messages("s", new_generation) == ()
    now[0] += timedelta(seconds=60)
    assert memory.ensure_generation("s") != new_generation


async def test_pending_claim_blocks_resend_and_finish_is_idempotent(stores):
    """平台发送前先落盘，状态未知时禁止任务重试导致重复发送。"""
    from agent_bot.sent_messages import SentMessageUncertainError

    memory, sent, _ = stores
    generation = memory.ensure_generation("s")
    values = dict(session_id="s", generation_id=generation, delivery_id="a" * 64,
                  request_id=None, message_id=None, content="主动通知", material_ids=())
    assert await sent.claim_delivery(**values) is None
    assert await sent.list_messages("s", generation) == ()
    with pytest.raises(SentMessageUncertainError):
        await sent.claim_delivery(**values)
    completed = await sent.finish_delivery("a" * 64, "qq-1")
    assert completed.message_id == "qq-1"
    assert await sent.finish_delivery("a" * 64, "qq-1") == completed
    assert await sent.claim_delivery(**values) == completed
    assert await sent.list_messages("s", generation) == (completed,)


async def test_old_claim_may_finish_but_never_enters_new_generation(stores):
    """重置不删除审计记录，但原代际投递完成不能污染新上下文。"""
    memory, sent, _ = stores
    old_generation = memory.ensure_generation("s")
    await sent.claim_delivery("s", old_generation, "a" * 64, None, None, "旧结果", ())
    memory.clear("s")
    generation = memory.ensure_generation("s")
    await sent.finish_delivery("a" * 64, "qq-1")
    assert await sent.list_messages("s", old_generation) == ()
    assert await sent.list_messages("s", generation) == ()


def test_merge_replaces_summary_and_preserves_chronological_assistant_only_events():
    """摘要被真实分段替换；通知和后台完成不虚构用户提问。"""
    from agent_bot.sent_messages import SentMessage, merge_sent_messages

    context = ConversationContext(memory="摘要", generation_id="generation", messages=(
        ChatMessage(role="user", content="旧问题"),
        ChatMessage(role="assistant", content="旧答复"),
        ChatMessage(role="user", content="画图", history_request_id="r1"),
        ChatMessage(role="assistant", content="未实际发送摘要", history_request_id="r1"),
    ))
    at = datetime(2026, 10, 19)
    rows = (
        SentMessage("a" * 64, "r1", "正在处理", (), at),
        SentMessage("b" * 64, None, "主动提醒", (), at + timedelta(seconds=1)),
        SentMessage("c" * 64, "r1", "图片 material-1", ("material-1",),
                    at + timedelta(seconds=2)),
        SentMessage("d" * 64, "background", "后台文件 material-2", ("material-2",),
                    at + timedelta(seconds=3)),
    )
    merged = merge_sent_messages(context, rows)
    assert [(message.role, message.content) for message in merged.messages] == [
        ("user", "旧问题"), ("assistant", "旧答复"), ("user", "画图"),
        ("assistant", "正在处理"), ("assistant", "主动提醒"),
        ("assistant", "图片 material-1"), ("assistant", "后台文件 material-2"),
    ]
    assert merged.memory == "摘要" and merged.generation_id == "generation"
    assert merge_sent_messages(context, ()) is context


async def test_list_returns_latest_limit_in_chronological_order(stores):
    """有界读取保留最新消息且以实际完成时间排序。"""
    memory, sent, now = stores
    generation = memory.ensure_generation("s")
    for index in range(3):
        await sent.record("s", generation, str(index) * 64, None, None, str(index), ())
        now[0] += timedelta(seconds=1)
    assert [row.content for row in await sent.list_messages("s", generation, limit=2)] == [
        "1", "2",
    ]


def test_merge_keeps_legacy_history_before_new_notices_without_fake_users():
    """只有独立通知时，既有历史必须仍位于新通知之前。"""
    from agent_bot.sent_messages import SentMessage, merge_sent_messages

    context = ConversationContext(memory="", messages=(
        ChatMessage(role="user", content="旧问题"),
        ChatMessage(role="assistant", content="旧回答"),
    ))
    row = SentMessage("a" * 64, None, "新通知", (), datetime(2026, 10, 19))
    assert [(message.role, message.content)
            for message in merge_sent_messages(context, (row,)).messages] == [
        ("user", "旧问题"), ("assistant", "旧回答"), ("assistant", "新通知"),
    ]


@pytest.mark.parametrize("change", [
    {"session_id": "other"}, {"generation_id": "b" * 32}, {"request_id": "other"},
    {"content": "篡改正文"}, {"material_ids": ("foreign",)},
])
async def test_delivery_identity_cannot_change_after_completion(stores, change):
    """旧投递回执不能用于确认另一会话、请求、正文或素材。"""
    memory, sent, _ = stores
    values = dict(session_id="s", generation_id=memory.ensure_generation("s"),
                  delivery_id="a" * 64, request_id="r1", message_id=None,
                  content="原始消息", material_ids=())
    await sent.claim_delivery(**values)
    await sent.finish_delivery("a" * 64, "qq-1")
    with pytest.raises(ValueError, match="identity conflicts"):
        await sent.claim_delivery(**(values | change))


async def test_concurrent_claims_allow_exactly_one_sender(stores):
    """唯一索引在真实并发连接间仲裁，不能同时取得两次发送授权。"""
    import asyncio

    from agent_bot.sent_messages import SentMessageUncertainError

    memory, sent, _ = stores
    generation = memory.ensure_generation("s")
    results = await asyncio.gather(*(
        sent.claim_delivery("s", generation, "a" * 64, None, None, "消息", ())
        for _ in range(2)
    ), return_exceptions=True)
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, SentMessageUncertainError) for result in results) == 1


def test_sqlite_manual_migration_matches_fresh_schema(tmp_path):
    """既有库增量升级与完整初始化得到相同字段、索引和约束。"""
    initialization = Path("sql/legacy/session-context-v1.sqlite.sql").read_text("utf-8")
    migration = Path("sql/2026-10-19-sent-messages.sqlite.sql").read_text("utf-8")
    schemas = []
    for name, scripts in (("fresh", (initialization,)),
                          ("upgrade", (initialization, "DROP TABLE chat_sent_message", migration))):
        with sqlite3.connect(tmp_path / f"{name}.db") as connection:
            for script in scripts:
                connection.executescript(script)
            schemas.append(connection.execute(
                "SELECT type,name,sql FROM sqlite_master WHERE name='chat_sent_message' "
                "OR tbl_name='chat_sent_message' ORDER BY type,name"
            ).fetchall())
    assert schemas[0] == schemas[1]


async def test_model_view_caps_full_file_and_keeps_material_ids_without_changing_journal(stores):
    """超长文件只裁切模型预览，全部原件标识和数据库完整正文仍可读取。"""
    from agent_bot.sent_messages import merge_sent_messages

    memory, sent, _ = stores
    generation = memory.ensure_generation("s")
    original = "full generated file\n" * 5000
    material_ids = ("original-file-one", "original-file-two")
    await sent.record("s", generation, "a" * 64, None, "qq-1", original, material_ids)
    rows = await sent.list_messages("s", generation)

    view = merge_sent_messages(memory.context("s"), rows)

    assert len(view.messages) == 1
    assert len(view.messages[0].content) <= 32000
    assert view.messages[0].content.startswith("full generated file\n")
    assert "material_get" in view.messages[0].content
    assert all(material_id in view.messages[0].content for material_id in material_ids)
    assert rows[0].content == original
    assert (await sent.list_messages("s", generation))[0].content == original


def test_model_context_drops_oldest_messages_to_configured_total_without_fake_users():
    """总量超限只保留最近消息，独立助手事件不因裁切而产生虚构用户。"""
    from agent_bot.sent_messages import SentMessage, merge_sent_messages

    context = ConversationContext(memory="摘要", generation_id="generation", messages=(
        ChatMessage(role="user", content="旧问题"),
        ChatMessage(role="assistant", content="旧回答"),
    ))
    at = datetime(2026, 10, 19)
    rows = tuple(SentMessage(str(index) * 64, None, content, (), at + timedelta(seconds=index))
                 for index, content in enumerate(("a" * 12, "b" * 12, "c" * 12)))

    view = merge_sent_messages(context, rows, max_chars=24, max_message_chars=20)

    assert [(message.role, message.content) for message in view.messages] == [
        ("assistant", "b" * 12), ("assistant", "c" * 12),
    ]
    assert sum(len(message.content) for message in view.messages) == 24
    assert view.memory == "摘要" and view.generation_id == "generation"


def test_default_model_window_bounds_all_messages_including_legacy_history():
    """日志混合旧轮次后，所有模型消息也遵守单条 32000 和合计 80000 限制。"""
    from agent_bot.sent_messages import SentMessage, merge_sent_messages

    context = ConversationContext(memory="", messages=(
        ChatMessage(role="user", content="user" * 25000),
        ChatMessage(role="assistant", content="assistant" * 12000),
    ))
    rows = tuple(SentMessage(str(index) * 64, None, str(index) * 40000, (),
                             datetime(2026, 10, 19) + timedelta(seconds=index))
                 for index in range(3))

    view = merge_sent_messages(context, rows)

    assert view.messages
    assert all(len(message.content) <= 32000 for message in view.messages)
    assert sum(len(message.content) for message in view.messages) <= 80000
    assert view.messages[-1].content.startswith("2" * 100)


def test_empty_journal_preserves_legacy_context_even_with_small_model_limits():
    """没有出站日志的兼容路径仍原样返回上下文，不改变既有历史策略。"""
    from agent_bot.sent_messages import merge_sent_messages

    context = ConversationContext(memory="旧摘要", messages=(
        ChatMessage(role="user", content="旧完整问题"),
        ChatMessage(role="assistant", content="旧完整回答"),
    ))
    assert merge_sent_messages(context, (), max_chars=1, max_message_chars=1) is context


@pytest.mark.parametrize("original", [
    "中文生成正文" * 80_000,
    "🚀" * 1_000_000 + "\n[文件 素材ID=original-file 名称=完整文件.txt]",
], ids=["chinese-over-mib", "unicode-limit-with-reference"])
async def test_durable_journal_accepts_reply_character_limit_with_unicode_and_reference(
    stores, original,
):
    """一百万字符回复按字符而非一 MiB 字节校验，四字节 Unicode 也保留全文。"""
    memory, sent, _ = stores
    generation = memory.ensure_generation("s")
    assert len(original.encode("utf-8")) > 1_048_576

    assert await sent.record("s", generation, "a" * 64, None, None, original,
                             ("original-file",))

    rows = await sent.list_messages("s", generation)
    assert rows[0].content == original
    assert rows[0].material_ids == ("original-file",)


async def test_durable_journal_rejects_content_beyond_reply_and_reference_allowance(stores):
    """素材引用的有界预留不能让日志接受任意超出回复契约的多百万字符正文。"""
    memory, sent, _ = stores
    generation = memory.ensure_generation("s")
    with pytest.raises(ValueError, match="invalid sent message content"):
        await sent.record("s", generation, "a" * 64, None, None, "x" * 1_032_001, ())
    assert await sent.list_messages("s", generation) == ()


@pytest.fixture
def outgoing_receipts(tmp_path):
    """队列回执使用独立文件和正式结构，模拟业务日志库与队列库分别提交。"""
    from agent_bot.integrations.sqlite_tool_operations import SqliteToolOperationStore

    path = tmp_path / "outgoing-receipts.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            Path("sql/agent_bot_queue_init.sqlite.sql").read_text(encoding="utf-8")
        )
    return SqliteToolOperationStore(str(path), key_prefix="sent-reconcile", ttl_seconds=60)


def reconciling_store(sent, now, receipts):
    """重建日志对象与回执对象，保证恢复不依赖发送协程或内存确认缓存。"""
    from agent_bot.integrations.sqlite_tool_operations import SqliteToolOperationStore
    from agent_bot.sent_messages import SentMessageStore

    restored_receipts = SqliteToolOperationStore(
        receipts.path, key_prefix="sent-reconcile", ttl_seconds=60,
    )
    return SentMessageStore(
        sent.params, connect=sent.connect, clock=lambda: now[0],
        receipts_provider=lambda: restored_receipts,
    )


async def test_list_reconciles_confirmed_sqlite_receipt_without_retrying_send(
    stores, outgoing_receipts,
):
    """日志仍为占位但平台回执已持久化时，单纯读取即可恢复真实成功消息。"""
    from agent_bot.services.tool_operations import DeliveryEffect

    memory, sent, now = stores
    generation = memory.ensure_generation("s")
    values = dict(session_id="s", generation_id=generation, delivery_id="a" * 64,
                  request_id="r1", message_id=None, content="独立通知和文件 material-report",
                  material_ids=("material-report",))
    assert await sent.claim_delivery(**values) is None
    signature = "b" * 64
    await outgoing_receipts.complete(
        "a" * 64, "outgoing_message", signature,
        DeliveryEffect(signature=signature, message_id="qq-confirmed"),
    )
    restored = reconciling_store(sent, now, outgoing_receipts)

    rows = await restored.list_messages("s", generation)

    assert [(row.delivery_id, row.request_id, row.message_id, row.content, row.material_ids)
            for row in rows] == [
        ("a" * 64, "r1", "qq-confirmed", "独立通知和文件 material-report", ("material-report",)),
    ]
    assert await restored.claim_delivery(**values) == rows[0]
    assert await restored.list_messages("s", generation) == rows


@pytest.mark.parametrize("receipt_state", ["missing", "running"])
async def test_reconcile_keeps_unconfirmed_pending_invisible(
    stores, outgoing_receipts, receipt_state,
):
    """缺失或仅有执行中回执都不是平台确认，读取不能把占位伪装成成功。"""
    from agent_bot.sent_messages import SentMessageUncertainError

    memory, sent, now = stores
    generation = memory.ensure_generation("s")
    values = dict(session_id="s", generation_id=generation, delivery_id="a" * 64,
                  request_id=None, message_id=None, content="未经确认的消息", material_ids=())
    await sent.claim_delivery(**values)
    if receipt_state == "running":
        await outgoing_receipts.begin("a" * 64, "outgoing_message", "b" * 64)
    restored = reconciling_store(sent, now, outgoing_receipts)

    assert await restored.list_messages("s", generation) == ()
    with pytest.raises(SentMessageUncertainError):
        await restored.claim_delivery(**values)


async def test_receipt_reconciliation_does_not_mix_reset_generations(stores, outgoing_receipts):
    """重置前后都有确认回执时，自动对账也只能读取当前代际的消息。"""
    from agent_bot.services.tool_operations import DeliveryEffect

    memory, sent, now = stores
    old_generation = memory.ensure_generation("s")
    await sent.claim_delivery("s", old_generation, "a" * 64, "old-request", None,
                              "旧后台结果 material-old", ("material-old",))
    memory.clear("s")
    generation = memory.ensure_generation("s")
    assert generation != old_generation
    await sent.claim_delivery("s", generation, "b" * 64, None, None, "当前独立通知", ())
    for delivery_id, message_id in (("a" * 64, "qq-old"), ("b" * 64, "qq-current")):
        await outgoing_receipts.complete(
            delivery_id, "outgoing_message", "c" * 64,
            DeliveryEffect(signature="c" * 64, message_id=message_id),
        )
    restored = reconciling_store(sent, now, outgoing_receipts)

    rows = await restored.list_messages("s", generation)

    assert [(row.content, row.message_id) for row in rows] == [("当前独立通知", "qq-current")]
    assert await restored.list_messages("s", old_generation) == ()
    assert await restored.list_messages("s", generation) == rows
