from __future__ import annotations

import pytest

from agent_bot.services.chat_tools import ChatToolContext
from agent_bot.services.tool_sessions import (
    ToolSessionCapacityError,
    ToolSessionExpiredError,
    ToolSessionStore,
)


@pytest.mark.asyncio
async def test_open_issues_an_opaque_token_and_revokes_it_on_exit() -> None:
    store = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    context = ChatToolContext(
        session_id="group:123",
        user_id="10001",
        allowed_tools=frozenset({"naraka_query"}),
    )

    async with store.open(context) as token:
        assert len(token) >= 32
        assert "group:123" not in token
        assert await store.resolve(token) is context

    with pytest.raises(ToolSessionExpiredError):
        await store.resolve(token)


@pytest.mark.asyncio
async def test_expired_tokens_are_rejected_and_capacity_is_reclaimed() -> None:
    now = 100.0
    store = ToolSessionStore(ttl_seconds=5, max_sessions=1, clock=lambda: now)
    context = ChatToolContext(
        session_id="private:10001",
        user_id="10001",
        allowed_tools=frozenset({"naraka_search"}),
    )

    first = await store.create(context)
    with pytest.raises(ToolSessionCapacityError):
        await store.create(context)

    now = 106.0
    with pytest.raises(ToolSessionExpiredError):
        await store.resolve(first)
    second = await store.create(context)
    assert second != first


@pytest.mark.asyncio
async def test_resolve_keeps_the_per_request_allow_list() -> None:
    store = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    context = ChatToolContext(
        session_id="group:123",
        user_id="10001",
        allowed_tools=frozenset({"mention_member"}),
    )
    token = await store.create(context)

    resolved = await store.resolve(token)

    assert resolved.allowed_tools == frozenset({"mention_member"})
    await store.revoke(token)


@pytest.mark.asyncio
async def test_resolve_slides_expiry_for_bounded_multi_step_agent_calls() -> None:
    now = 100.0
    store = ToolSessionStore(ttl_seconds=5, max_sessions=1, clock=lambda: now)
    context = ChatToolContext(
        session_id="private:10001",
        user_id="10001",
        allowed_tools=frozenset({"local_workflow_generate_image"}),
    )
    token = await store.create(context)

    now = 104.0
    assert await store.resolve(token) is context
    now = 108.0
    assert await store.resolve(token) is context
    now = 114.0
    with pytest.raises(ToolSessionExpiredError):
        await store.resolve(token)
