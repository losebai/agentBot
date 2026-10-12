from __future__ import annotations

import importlib

import pytest


def _store_type():
    module = importlib.import_module("agent_bot.group_context")
    return module.RecentGroupContextStore


def test_store_returns_only_latest_messages_in_original_order() -> None:
    store = _store_type()(
        message_limit=2,
        max_chars=1000,
        ttl_seconds=60,
        max_groups=10,
    )

    store.record(
        bot_id="90001",
        group_id="20001",
        sender_id="10001",
        sender_name="小明",
        content="第一条",
    )
    store.record(
        bot_id="90001",
        group_id="20001",
        sender_id="10002",
        sender_name="小红",
        content="第二条",
    )
    store.record(
        bot_id="90001",
        group_id="20001",
        sender_id="10003",
        sender_name=None,
        content="第三条",
    )

    assert store.context(bot_id="90001", group_id="20001") == (
        "小红(QQ 10002)：第二条\nQQ 10003：第三条"
    )


def test_store_isolates_context_by_bot_and_group() -> None:
    store = _store_type()(
        message_limit=10,
        max_chars=1000,
        ttl_seconds=60,
        max_groups=10,
    )
    store.record(
        bot_id="90001",
        group_id="20001",
        sender_id="10001",
        sender_name="本群成员",
        content="本群消息",
    )
    store.record(
        bot_id="90001",
        group_id="20002",
        sender_id="10002",
        sender_name="其他群成员",
        content="其他群消息",
    )
    store.record(
        bot_id="90002",
        group_id="20001",
        sender_id="10003",
        sender_name="其他机器人消息",
        content="其他机器人上下文",
    )

    assert store.context(bot_id="90001", group_id="20001") == (
        "本群成员(QQ 10001)：本群消息"
    )


def test_store_bounds_rendered_context_by_character_limit() -> None:
    store = _store_type()(
        message_limit=10,
        max_chars=30,
        ttl_seconds=60,
        max_groups=10,
    )

    store.record(
        bot_id="90001",
        group_id="20001",
        sender_id="10001",
        sender_name="小明",
        content="甲" * 100,
    )

    context = store.context(bot_id="90001", group_id="20001")
    assert context is not None
    assert len(context) == 30
    assert context.startswith("小明(QQ 10001)：")


def test_store_expires_group_context_at_ttl_boundary() -> None:
    now = [100.0]
    store = _store_type()(
        message_limit=10,
        max_chars=1000,
        ttl_seconds=30,
        max_groups=10,
        clock=lambda: now[0],
    )
    store.record(
        bot_id="90001",
        group_id="20001",
        sender_id="10001",
        sender_name="小明",
        content="仍在吗",
    )

    now[0] = 129.999
    assert store.context(bot_id="90001", group_id="20001") is not None

    now[0] = 130.0
    assert store.context(bot_id="90001", group_id="20001") is None


def test_store_evicts_least_recently_used_group() -> None:
    store = _store_type()(
        message_limit=10,
        max_chars=1000,
        ttl_seconds=60,
        max_groups=2,
    )
    for group_id in ("20001", "20002"):
        store.record(
            bot_id="90001",
            group_id=group_id,
            sender_id="10001",
            sender_name="小明",
            content=group_id,
        )
    assert store.context(bot_id="90001", group_id="20001") is not None

    store.record(
        bot_id="90001",
        group_id="20003",
        sender_id="10001",
        sender_name="小明",
        content="新群",
    )

    assert store.context(bot_id="90001", group_id="20002") is None
    assert store.context(bot_id="90001", group_id="20001") is not None
    assert store.context(bot_id="90001", group_id="20003") is not None


def test_store_does_not_create_empty_group_for_oversized_sender_label() -> None:
    store = _store_type()(
        message_limit=10,
        max_chars=20,
        ttl_seconds=60,
        max_groups=1,
    )

    for group_id in ("20001", "20002"):
        store.record(
            bot_id="90001",
            group_id=group_id,
            sender_id="10001",
            sender_name="超长群昵称" * 10,
            content="消息",
        )

    assert len(store._groups) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("message_limit", 0),
        ("max_chars", 0),
        ("ttl_seconds", 0),
        ("max_groups", 0),
    ],
)
def test_store_rejects_non_positive_limits(field: str, value: int) -> None:
    limits = {
        "message_limit": 10,
        "max_chars": 1000,
        "ttl_seconds": 60,
        "max_groups": 10,
    }
    limits[field] = value

    with pytest.raises(ValueError):
        _store_type()(**limits)
