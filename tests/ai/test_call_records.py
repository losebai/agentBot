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
    """普通记录与统一记录同时保存，正文只在详情返回。"""
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


async def test_missing_unified_table_rolls_back_old_write(ai_params, unified_db):
    """新表写失败时旧会话也不能单独提交，保证调用方获知保存失败。"""
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
