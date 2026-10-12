from datetime import datetime

import pytest

from agent_bot.ai.contracts import SessionWrite, TokenUsage
from agent_bot.ai.errors import AiError
from agent_bot.ai.sessions import AiSessionStore


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
