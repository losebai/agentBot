from agent_bot.models import ChatMessage
from agent_bot.state import (
    AccessPolicy,
    ConversationStore,
    build_session_scope,
    external_session_id,
    split_reply,
)


def test_private_and_group_session_scopes_are_isolated() -> None:
    assert build_session_scope(bot_id="9", user_id="1") == "bot:9:private:1"
    assert build_session_scope(bot_id="9", user_id="1", group_id="10") == "bot:9:group:10:user:1"
    assert build_session_scope(bot_id="9", user_id="2", group_id="10") == "bot:9:group:10:user:2"
    assert (
        build_session_scope(bot_id="9", user_id="1", group_id="10", group_shared=True)
        == "bot:9:group:10"
    )


def test_external_session_id_is_stable_and_hides_qq_identifiers() -> None:
    scope = "bot:111:group:123456:user:987654"
    secret = "test-secret"
    session_id = external_session_id(scope, secret)

    assert session_id == external_session_id(scope, secret)
    assert session_id != external_session_id(scope, "different-secret")
    assert session_id.startswith("qq_")
    assert "123456" not in session_id
    assert "987654" not in session_id


def test_access_policy_uses_private_allowlist_and_group_blocklists() -> None:
    policy = AccessPolicy(
        allowed_users={"1"},
        blocked_users={"2"},
        blocked_groups={"20"},
    )

    assert policy.allows(user_id="1", group_id="10") is True
    assert policy.allows(user_id="1") is True
    assert policy.allows(user_id="2", group_id="10") is False
    assert policy.allows(user_id="1", group_id="20") is False


def test_empty_access_policy_denies_by_default() -> None:
    policy = AccessPolicy()

    assert policy.allows(user_id="1") is False
    assert policy.allows(user_id="1", group_id="10") is True


def test_access_policy_can_explicitly_allow_all() -> None:
    policy = AccessPolicy(allow_all=True)

    assert policy.allows(user_id="1") is True
    assert policy.allows(user_id="1", group_id="10") is True


def test_access_policy_repr_does_not_expose_qq_identifiers() -> None:
    policy = AccessPolicy(allowed_users={"private-user-id"})

    assert "private-user-id" not in repr(policy)


def test_store_preserves_complete_turns_and_trims_oldest_turn() -> None:
    store = ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60)

    store.commit_exchange("s1", "u1", "a1")
    store.commit_exchange("s1", "u2", "a2")
    store.commit_exchange("s1", "u3", "a3")

    assert store.messages("s1") == [
        ChatMessage(role="user", content="u2"),
        ChatMessage(role="assistant", content="a2"),
        ChatMessage(role="user", content="u3"),
        ChatMessage(role="assistant", content="a3"),
    ]


def test_store_batches_old_turns_into_bounded_mental_memory() -> None:
    store = ConversationStore(
        history_turns=4,
        max_sessions=10,
        ttl_seconds=60,
        memory_enabled=True,
        max_memory_chars=80,
    )

    for index in range(1, 5):
        assert store.commit_exchange("s1", f"u{index}", f"a{index}") is None
    compression = store.commit_exchange("s1", "u5", "a5")

    assert compression is not None
    assert compression.previous_memory == ""
    assert [(turn.user, turn.assistant) for turn in compression.turns] == [
        ("u1", "a1"),
        ("u2", "a2"),
        ("u3", "a3"),
    ]
    assert store.messages("s1") == [
        ChatMessage(role="user", content="u4"),
        ChatMessage(role="assistant", content="a4"),
        ChatMessage(role="user", content="u5"),
        ChatMessage(role="assistant", content="a5"),
    ]
    assert "u1" in store.memory("s1")
    assert len(store.memory("s1")) <= 80

    assert store.apply_memory(compression, "用户喜欢喝茶，并希望回答简洁。") is True
    assert store.memory("s1") == "用户喜欢喝茶，并希望回答简洁。"


def test_store_rejects_stale_memory_compression_result() -> None:
    store = ConversationStore(
        history_turns=1,
        max_sessions=10,
        ttl_seconds=60,
        memory_enabled=True,
        max_memory_chars=20,
    )
    store.commit_exchange("s1", "u1", "a1")
    first = store.commit_exchange("s1", "u2", "a2")
    second = store.commit_exchange("s1", "u3", "a3")

    assert first is not None
    assert second is not None
    assert store.apply_memory(first, "过期摘要") is False
    assert store.apply_memory(second, "新摘要超过长度限制" * 10) is True
    assert "过期摘要" not in store.memory("s1")
    assert len(store.memory("s1")) == 20


def test_store_memory_expires_and_is_cleared_with_the_session() -> None:
    now = [100.0]
    store = ConversationStore(
        history_turns=0,
        max_sessions=10,
        ttl_seconds=30,
        memory_enabled=True,
        max_memory_chars=80,
        clock=lambda: now[0],
    )
    compression = store.commit_exchange("s1", "remember me", "noted")

    assert compression is not None
    assert store.apply_memory(compression, "用户要求记住这件事") is True
    assert store.memory("s1") == "用户要求记住这件事"

    now[0] = 130.0
    assert store.memory("s1") == ""
    assert store.clear("s1") is False


def test_store_expires_history_at_ttl_boundary() -> None:
    now = [100.0]
    store = ConversationStore(
        history_turns=2,
        max_sessions=10,
        ttl_seconds=30,
        clock=lambda: now[0],
    )
    store.commit_exchange("s1", "hello", "world")

    now[0] = 129.999
    assert store.messages("s1")

    now[0] = 130.0
    assert store.messages("s1") == []


def test_store_evicts_least_recently_used_session() -> None:
    store = ConversationStore(history_turns=1, max_sessions=2, ttl_seconds=60)
    store.commit_exchange("s1", "u1", "a1")
    store.commit_exchange("s2", "u2", "a2")
    assert store.messages("s1")

    store.commit_exchange("s3", "u3", "a3")

    assert store.messages("s2") == []
    assert store.messages("s1")
    assert store.messages("s3")


def test_store_clear_is_idempotent() -> None:
    store = ConversationStore(history_turns=1, max_sessions=2, ttl_seconds=60)
    store.commit_exchange("s1", "u", "a")

    assert store.clear("s1") is True
    assert store.clear("s1") is False
    assert store.messages("s1") == []


def test_split_reply_preserves_full_text_when_not_truncated() -> None:
    text = "第一段\n第二段很长\n第三段"

    chunks = split_reply(text, chunk_chars=8, max_reply_chars=100)

    assert "".join(chunks) == text
    assert all(0 < len(chunk) <= 8 for chunk in chunks)


def test_split_reply_truncates_with_explicit_marker() -> None:
    chunks = split_reply("甲" * 50, chunk_chars=10, max_reply_chars=25)
    result = "".join(chunks)

    assert len(result) <= 25
    assert result.endswith("[内容过长，已截断]")


def test_split_reply_rejects_empty_text() -> None:
    assert split_reply("", chunk_chars=10, max_reply_chars=20) == []
