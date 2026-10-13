from datetime import datetime

import pytest

from agent_bot.ai.contracts import SessionWrite, TokenUsage
from agent_bot.ai.errors import AiError
from agent_bot.ai.sessions import AiSessionStore
from ai.mysql_identifiers import reject_unquoted_usage


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
