"""用隔离数据库核验记忆总量、召回计数和 UTC 区间边界。"""

from datetime import datetime, timedelta

from test_tagged_memory import memory_store as memory_store

from agent_bot.memory.policy import utc_now
from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity


def test_memory_statistics_count_current_active_facts_and_completed_recalls(memory_store):
    """多条命中只计一次，零命中进入分母，目录读取不影响召回率。"""
    identity = MemoryIdentity(bot_id="bot", user_id="user", session_id="s", request_id="req")
    for key in ("first", "second"):
        memory_store.execute(identity, MemoryArguments(
            action="save", key=key, content=key, tags=["命中"],
        ))
    now = utc_now()
    for key, values in (
        ("expired", {"valid_until": now - timedelta(days=1)}),
        ("future", {"valid_from": now + timedelta(days=1)}),
        ("pending", {"status": "pending"}),
        ("deleted", {"is_deleted": 1}),
        ("disputed", {"status": "disputed"}),
        ("merged", {"status": "merged"}),
    ):
        record = memory_store.execute(identity, MemoryArguments(
            action="save", key=key, content=key, tags=["其他"],
        )).records[0]
        with memory_store._transaction() as cursor:
            cursor.execute(
                "UPDATE chat_tagged_memory SET "
                + ",".join(f"{column}=%s" for column in values) + " WHERE id=%s",
                (*values.values(), record.id),
            )
    memory_store.execute(identity, MemoryArguments(action="recall", tags=["命中"]))
    memory_store.execute(identity, MemoryArguments(action="recall", tags=["未命中"]))
    memory_store.execute(identity, MemoryArguments(action="tags", scope="all"))
    # 语义和词项检索共享的结果提交入口也必须记录零命中。
    memory_store.semantic_result(identity, [])
    stats = memory_store.admin_statistics(
        start_time=now - timedelta(days=1), end_time=now + timedelta(days=1),
    )
    assert stats.memory_count == 2
    assert stats.recall_count == 3
    assert stats.recall_hit_count == 1
    assert stats.recall_rate == 1 / 3


def test_memory_statistics_half_open_range_and_legacy_receipts(memory_store):
    """UTC 左闭右开；旧成功引用不能推算未命中分母，无样本保持未知。"""
    identity = MemoryIdentity(bot_id="bot", user_id="user", session_id="s", request_id="req")
    record = memory_store.execute(identity, MemoryArguments(
        action="save", key="fact", content="事实", tags=["命中"],
    )).records[0]
    memory_store.execute(identity, MemoryArguments(action="recall", tags=["命中"]))
    memory_store.execute(identity, MemoryArguments(action="recall", tags=["未命中"]))
    start, end = datetime(2026, 10, 29, 16), datetime(2026, 10, 30, 16)
    with memory_store._transaction() as cursor:
        cursor.execute(
            "SELECT operation_id FROM chat_memory_operation "
            "WHERE operation_kind='recall_attempt' ORDER BY operation_id",
        )
        receipts = cursor.fetchall()
        assert len(receipts) == 2
        for receipt, stamp in zip(receipts, (start, end), strict=True):
            cursor.execute(
                "UPDATE chat_memory_operation SET created_at=%s WHERE operation_id=%s",
                (stamp, receipt["operation_id"]),
            )
    stats = memory_store.admin_statistics(start_time=start, end_time=end)
    assert stats.memory_count == 1
    assert stats.recall_count == 1
    with memory_store._transaction() as cursor:
        cursor.execute("DELETE FROM chat_memory_operation WHERE operation_kind='recall_attempt'")
    stats = memory_store.admin_statistics(start_time=start, end_time=end)
    assert stats.recall_count == stats.recall_hit_count == 0
    assert stats.recall_rate is None
    assert memory_store.admin_get(record.id).content == "事实"
