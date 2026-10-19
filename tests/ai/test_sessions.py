from datetime import datetime

import pytest

from agent_bot.ai.contracts import SessionWrite, TokenUsage
from agent_bot.ai.errors import AiError
from agent_bot.ai.sessions import AiSessionStore
from ai.mysql_identifiers import reject_unquoted_usage


def merged_record(**updates) -> SessionWrite:
    """构造仅用于隔离归档测试的普通调用，不连接模型。"""
    values = {
        "execution_id": "a" * 32, "session_id": "session", "request_id": "request",
        "configured_model": "alias", "model": "upstream", "status": "completed",
        "duration_ms": 15, "usage": TokenUsage(total_tokens=7),
        "created_at": datetime(2026, 10, 20, 1, 2, 3, 123456),
        "prompt": "输入", "content": "输出",
    }
    return SessionWrite(**(values | updates))


async def test_ordinary_records_work_after_legacy_table_is_removed(ai_params, ai_database):
    """删除旧普通表后保存、列表和详情只依赖统一归档，完整保留 UTC 微秒。"""
    ai_database.connection.execute("DROP TABLE agent_bot_ai_session")
    store = AiSessionStore(ai_params, connect=ai_database.connect)
    record = merged_record()
    await store.save(record)
    detail = await store.get_record(record.execution_id)
    assert detail.model_dump() == record.model_dump(exclude={"request_context"})
    assert (await store.list_records()).items[0].created_at == record.created_at
    assert ai_database.connection.execute(
        "SELECT COUNT(*) FROM agent_bot_call_record",
    ).fetchone()[0] == 1


async def test_microsecond_paging_keeps_original_order_and_excludes_other_sources(
    ai_params, ai_database,
):
    """同秒调用按 UTC 微秒排序，普通接口不会混入 Agent 或其他来源。"""
    ai_database.connection.execute("DROP TABLE agent_bot_ai_session")
    store = AiSessionStore(ai_params, connect=ai_database.connect)
    await store.save(merged_record(execution_id="f" * 32))
    await store.save(merged_record(
        execution_id="b" * 32, created_at=datetime(2026, 10, 20, 1, 2, 3, 123457),
    ))
    first = await store.list_records(limit=1)
    second = await store.list_records(limit=1, cursor=first.next_cursor)
    assert [first.items[0].execution_id, second.items[0].execution_id] == ["b" * 32, "f" * 32]
    assert second.next_cursor is None


async def test_full_text_and_256_character_identities_survive_unified_storage(
    ai_params, ai_database,
):
    """统一表兼容扩展中的长正文和身份可还原，列表按完整会话标识精确过滤。"""
    ai_database.connection.execute("DROP TABLE agent_bot_ai_session")
    store = AiSessionStore(ai_params, connect=ai_database.connect)
    record = merged_record(
        session_id="s" * 256, request_id="r" * 256,
        prompt="长输入" * 30000, content="长输出" * 30000,
        status="failed", error_code="AI_TIMEOUT",
    )
    await store.save(record)
    detail = await store.get_record(record.execution_id)
    assert detail.model_dump() == record.model_dump(exclude={"request_context"})
    summary = (await store.list_records(session_id=record.session_id, status="failed")).items[0]
    assert summary.session_id == record.session_id
    assert summary.request_id == record.request_id
    assert summary.usage == record.usage
    assert not (await store.list_records(session_id=record.session_id[:255])).items
    assert record.prompt not in summary.model_dump_json()


async def test_record_paging_and_redaction(ai_params, ai_database):
    store = AiSessionStore(ai_params, connect=ai_database.connect)
    for execution_id in ("a" * 32, "b" * 32):
        await store.save(
            SessionWrite(
                execution_id=execution_id,
                session_id="s",
                request_id="r",
                configured_model="local",
                model="exact/model",
                status="completed",
                duration_ms=1,
                usage=TokenUsage(),
                created_at=datetime(2026, 10, 12),
                prompt="private input",
                content="private output",
            )
        )
    first = await store.list_records(session_id="s", status=None, limit=1, cursor=None)
    second = await store.list_records(
        session_id="s", status=None, limit=1, cursor=first.next_cursor
    )
    assert [first.items[0].execution_id, second.items[0].execution_id] == ["b" * 32, "a" * 32]
    assert "private input" not in first.model_dump_json()
    assert (await store.get_record("a" * 32)).content == "private output"
    with pytest.raises(AiError):
        await store.list_records(session_id=None, status=None, limit=101, cursor=None)


@pytest.mark.parametrize("mysql_semantics", [False, True], ids=["sqlite", "mysql"])
async def test_reserved_usage_roundtrips_session_records(
    ai_params, ai_database, monkeypatch, mysql_semantics
):
    """AI 会话的 token 用量可跨后端保存、列出并读取详情。"""
    if mysql_semantics:
        reject_unquoted_usage(monkeypatch)
    store = AiSessionStore(ai_params, connect=ai_database.connect)
    await store.save(
        SessionWrite(
            execution_id="c" * 32,
            session_id="usage-session",
            request_id="request",
            configured_model="local",
            model="exact/model",
            status="completed",
            duration_ms=1,
            usage=TokenUsage(prompt_tokens=4, completion_tokens=3, total_tokens=7),
            created_at=datetime(2026, 10, 12),
            prompt="private input",
            content="answer",
        )
    )
    summary = (await store.list_records(session_id="usage-session")).items[0]
    detail = await store.get_record("c" * 32)
    assert summary.usage.total_tokens == detail.usage.total_tokens == 7
    assert detail.content == "answer"
