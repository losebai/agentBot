"""统一记录覆盖事务一致性、历史去重、类型识别及凭据清理。"""

from datetime import datetime
from pathlib import Path

import pytest

from agent_bot.ai.call_records import CallRecordStore, legacy_record, sanitize_context
from agent_bot.ai.contracts import SessionWrite, TokenUsage
from agent_bot.ai.errors import AiError
from agent_bot.ai.sessions import AiSessionStore
from agent_bot.record_migration import import_jsonl


@pytest.fixture
def unified_db(ai_database):
    """使用真实 SQLite 约束验证合并，不访问外部数据库。"""
    sql = Path("sql/2026-10-13-unified-call-records.sqlite.sql").read_text(encoding="utf-8")
    ai_database.connection.executescript(sql)
    return ai_database


async def test_new_calls_share_table_and_keep_field_meanings(ai_params, unified_db):
    """普通调用仅写统一记录，正文只在详情返回。"""
    old = AiSessionStore(ai_params, connect=unified_db.connect)
    await old.save(
        SessionWrite(
            execution_id="a" * 32,
            session_id="s",
            request_id="r",
            configured_model="alias",
            model="upstream",
            status="completed",
            duration_ms=15,
            usage=TokenUsage(total_tokens=4),
            created_at=datetime(2026, 10, 13),
            prompt="输入",
            content="回复",
            request_context={"messages": [{"role": "user", "content": "输入"}]},
        )
    )
    store = CallRecordStore(ai_params, connect=unified_db.connect)
    page = await store.list_calls()
    assert len(page.items) == 1
    assert page.items[0].type == "ordinary"
    assert "content" not in page.items[0].model_dump()
    detail = await store.get_call(page.items[0].id)
    assert (detail.content, detail.replay_context, detail.duration) == ("输入", "回复", 15)
    assert detail.data["usage"]["total_tokens"] == 4


async def test_overview_statistics_sum_usage_once_and_weight_only_reported_cache(
    ai_params, unified_db,
):
    """按调用归档统计，系统调用计用量，未知缓存不稀释命中率，超长记录仍参与。"""
    store = CallRecordStore(ai_params, connect=unified_db.connect)
    usages = [
        {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
         "cached_tokens": 60},
        {"prompt_tokens": 200, "completion_tokens": 20, "cached_tokens": 50},
        {"prompt_tokens": 20, "completion_tokens": 2, "total_tokens": 22,
         "cached_tokens": 0},
        {"prompt_tokens": 1000, "completion_tokens": 1, "total_tokens": 1001},
        {},
        {"prompt_tokens": 50, "completion_tokens": 5, "cached_tokens": 0},
    ]
    rows = []
    for number, usage in enumerate(usages):
        data = {"choices": [], "usage": usage}
        if number == 1:
            data["toolCalls"] = []
        if number == 2:
            data["system_kind"] = "command"
        rows.append(legacy_record({
            "id": number, "session_Id": "同一会话", "data": data,
            "content": "长输入" * 30000 if number == 5 else "输入",
            "replay_context": "回答",
        }, "overview-test"))
    # 统一表是权威归档；不再累加原始普通与 Agent 执行表，避免重复计算。
    await store.import_batch(rows)
    stats = await store.statistics()
    assert stats.conversation_count == 5
    assert stats.total_tokens == 1408
    assert stats.prompt_tokens == 1370
    assert stats.completion_tokens == 38
    assert stats.cached_tokens == 110
    assert stats.cache_prompt_tokens == 370
    assert stats.cache_hit_rate == pytest.approx(110 / 370)


async def test_empty_or_unreported_cache_statistics_remain_unknown(ai_params, unified_db):
    """空库累计为零；只有输入用量但没有缓存报告时，命中率仍为未知。"""
    store = CallRecordStore(ai_params, connect=unified_db.connect)
    empty = await store.statistics()
    assert empty.conversation_count == empty.total_tokens == 0
    assert empty.cache_hit_rate is None
    await store.import_batch([legacy_record({
        "id": 1, "data": {"choices": [], "usage": {"prompt_tokens": 100}},
    }, "overview-test")])
    partial = await store.statistics()
    assert partial.cache_hit_rate is None
    assert partial.total_tokens == 100


@pytest.mark.parametrize(("usage", "overflow", "expected"), [
    ({"prompt_tokens": 1000, "completion_tokens": 100, "total_tokens": 1100,
      "cached_tokens": 300, "cache_prompt_tokens": 500}, False,
     TokenUsage(prompt_tokens=1000, completion_tokens=100, total_tokens=1100,
                cached_tokens=300, cache_prompt_tokens=500)),
    ({"prompt_tokens": 100, "completion_tokens": 20,
      "prompt_tokens_details": {"cached_tokens": 0}}, False,
     TokenUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120,
                cached_tokens=0, cache_prompt_tokens=100)),
    ({"input_tokens": 200, "output_tokens": 30,
      "input_tokens_details": {"cached_tokens": 80}}, True,
     TokenUsage(prompt_tokens=200, completion_tokens=30, total_tokens=230,
                cached_tokens=80, cache_prompt_tokens=200)),
    ({"prompt_tokens": 50, "completion_tokens": 5, "cached_tokens": None}, False,
     TokenUsage(prompt_tokens=50, completion_tokens=5, total_tokens=55)),
    ({"prompt_tokens": None, "input_tokens": 90, "completion_tokens": None,
      "cached_tokens": 0}, False,
     TokenUsage(prompt_tokens=90, cached_tokens=0, cache_prompt_tokens=90)),
    (None, False, TokenUsage()),
])
async def test_call_list_and_detail_expose_normalized_usage(
    ai_params, unified_db, usage, overflow, expected,
):
    """列表与详情统一新旧协议用量，超长归档、零命中和未上报字段保持真实含义。"""
    store = CallRecordStore(ai_params, connect=unified_db.connect)
    await store.import_batch([legacy_record({
        "id": 1, "content": "长输入" * 30000 if overflow else "输入",
        "data": {"choices": [], "usage": usage},
    }, "usage-test")])
    summary = (await store.list_calls()).items[0]
    assert summary.usage == expected
    assert "data" not in summary.model_dump()
    detail = await store.get_call(summary.id)
    assert detail.usage == expected


async def test_legacy_idempotency_and_type_bound_cursor(ai_params, unified_db):
    """来源隔离相同旧主键，重复导入不覆盖记录且游标不能换筛选。"""
    store = CallRecordStore(ai_params, connect=unified_db.connect)
    rows = [
        legacy_record({"id": n, "session_Id": "旧", "data": {"toolCalls": []}}, "legacy")
        for n in (1, 2)
    ]
    assert await store.import_batch(rows) == 2
    assert await store.import_batch(rows) == 0
    assert await store.import_batch([legacy_record({"id": 1}, "other")]) == 1
    page = await store.list_calls(type="agent", limit=1)
    assert page.next_cursor
    second = await store.list_calls(type="agent", limit=1, cursor=page.next_cursor)
    assert page.items[0].id != second.items[0].id
    with pytest.raises(AiError):
        await store.list_calls(type="ordinary", cursor=page.next_cursor)


def test_context_and_legacy_classification():
    """嵌套工具参数及 JSON 文本不得携带临时凭据。"""
    raw = {
        "toolSessionToken": "secret",
        "messages": [{"arguments": '{"api_key":"secret","query":"你好"}'}],
        "max_tokens": 99,
    }
    clean = sanitize_context(raw)
    assert "secret" not in str(clean)
    assert clean["max_tokens"] == 99
    assert legacy_record({"id": 1, "data": {"choices": []}}, "x")["type"] == "ordinary"
    assert legacy_record({"id": 2, "request_context": {"maxSteps": 3}}, "x")["type"] == "agent"
    assert legacy_record({"id": 3}, "x")["type"] == "unknown"


async def test_failed_batch_rolls_back(ai_params, unified_db):
    """批中失败不得留下半批导入数据。"""
    store = CallRecordStore(ai_params, connect=unified_db.connect)
    good = legacy_record({"id": 1}, "rollback")
    bad = {**legacy_record({"id": 2}, "rollback"), "type": None}
    with pytest.raises(AiError):
        await store.import_batch([good, bad])
    assert not (await store.list_calls()).items


async def test_chronological_pages_include_null_times(ai_params, unified_db):
    """后导入的旧记录不能压过新调用，空时间记录在末页且不遗漏。"""
    store = CallRecordStore(ai_params, connect=unified_db.connect)
    rows = [
        legacy_record({"id": i, "create_time": stamp}, "legacy")
        for i, stamp in (
            (1, "2026-10-13 12:00:00"),
            (2, "2026-10-12 00:00:00"),
            (3, None),
            (4, None),
        )
    ]
    await store.import_batch(rows)
    ids, cursor = [], None
    while True:
        page = await store.list_calls(limit=1, cursor=cursor)
        ids.extend(item.source_id for item in page.items)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert ids == ["1", "2", "4", "3"]


async def test_numbered_pages_and_totals_use_the_same_filters(ai_params, unified_db):
    """直接跳页保持时间和 ID 排序，计数先筛选，且末页、空页不混入其他分类。"""
    store = CallRecordStore(ai_params, connect=unified_db.connect)
    rows = [legacy_record({
        "id": number, "session_Id": "group-a", "status": "completed",
        "data": {"choices": [], "status": "completed"},
        "create_time": None if number < 3 else "2026-10-27 12:00:00",
    }, "numbered") for number in range(1, 6)]
    rows.extend([
        legacy_record({"id": 6, "session_Id": "group-b"}, "numbered"),
        legacy_record({"id": 7, "session_Id": "group-a", "request_id": "qq-summary-old"},
                      "numbered"),
    ])
    await store.import_batch(rows)
    filters = {"category": "conversation", "session_id": "group-a", "status": "completed"}
    assert (await store.count_calls(**filters)).total == 5
    assert (await store.count_calls(category="system")).total == 1
    assert (await store.count_calls(session_id="absent")).total == 0
    second = await store.list_calls(**filters, page=2, limit=2)
    assert [row.source_id for row in second.items] == ["3", "2"]
    assert second.next_cursor is not None
    last = await store.list_calls(**filters, page=3, limit=2)
    assert [row.source_id for row in last.items] == ["1"]
    assert last.next_cursor is None
    assert not (await store.list_calls(**filters, page=4, limit=2)).items
    assert "content" not in second.items[0].model_dump()


@pytest.mark.parametrize("options", [
    {"page": 0}, {"page": -1}, {"page": True}, {"page": 1.5},
    {"page": 1, "cursor": "cursor"},
])
async def test_numbered_pages_reject_invalid_or_mixed_pagination(ai_params, unified_db, options):
    """拒绝非法页码及页码与游标混用，不能把失败查询悄悄退回首页。"""
    store = CallRecordStore(ai_params, connect=unified_db.connect)
    with pytest.raises(AiError, match="AI_INVALID_PAGE"):
        await store.list_calls(**options)


async def test_jsonl_repeated_import_and_redaction(ai_params, unified_db, tmp_path):
    """导出文件分批导入后可重跑，凭据摘要及结构化脱敏都实际落库。"""
    import json

    store = CallRecordStore(ai_params, connect=unified_db.connect)
    path = tmp_path / "history.jsonl"
    rows = [
        {
            "id": 1,
            "session_Id": "legacy",
            "content": "输入",
            "replay_context": "输出",
            "user_certificate": "private-credential",
            "request_context": {"toolSessionToken": "temporary-secret", "maxSteps": 3},
        },
        {"id": 2, "data": {"type": "auto_speak_decision", "status": "failed"}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    assert await import_jsonl(store, path, "legacy", batch_size=1) == {"scanned": 2, "inserted": 2}
    assert await import_jsonl(store, path, "legacy", batch_size=1) == {"scanned": 2, "inserted": 0}
    detail = await store.get_call((await store.list_calls(type="agent")).items[0].id)
    assert detail.user_certificate.startswith("sha256:")
    assert "temporary-secret" not in detail.model_dump_json()
    assert "private-credential" not in detail.model_dump_json()
    assert (await store.list_calls(type="auto_speak")).items[0].status == "failed"


async def test_missing_unified_table_does_not_write_old_table(ai_params, unified_db):
    """统一归档不可用时明确失败，不再向旧表写入独立副本。"""
    unified_db.connection.execute("DROP TABLE agent_bot_call_record")
    store = AiSessionStore(ai_params, connect=unified_db.connect)
    with pytest.raises(AiError):
        await store.save(
            SessionWrite(
                execution_id="f" * 32,
                session_id="s",
                request_id="r",
                configured_model="a",
                model="m",
                status="completed",
                duration_ms=1,
                usage=TokenUsage(),
                created_at=datetime.now(),
                prompt="输入",
                content="回复",
            )
        )
    assert (
        unified_db.connection.execute("SELECT COUNT(*) FROM agent_bot_ai_session").fetchone()[0]
        == 0
    )
