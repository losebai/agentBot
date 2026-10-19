"""仅用明确的已知 QQ 目录身份生成管理员标题，不反查匿名标识或扩大权限。"""

from agent_bot.services.tagged_memory import memory_scope_owner
from agent_bot.state import build_session_scope, external_session_id


def test_titles_match_known_profiles_and_keep_bot_and_session_boundaries() -> None:
    """同名身份必须匹配机器人与完整 HMAC，私聊和群共享及个人会话都能识别。"""
    from agent_bot.knowledge import memory_titles

    resolver = getattr(memory_titles, "scope_titles", None)
    assert callable(resolver)
    secret = "offline-scope-title-secret"
    rows = [{"bot_id": "10001", "group_id": "20001", "user_id": "30001",
             "group_name": "读书群", "nickname": "小明", "card": "群内名"}]
    targets = {}
    for scope, subject in (("global", ""), ("user", "30001"), ("group", "20001")):
        targets[memory_scope_owner("10001", scope, subject, secret)] = scope
    for group, shared in ((None, False), ("20001", False), ("20001", True)):
        session = external_session_id(build_session_scope(
            bot_id="10001", user_id="30001", group_id=group, group_shared=shared,
        ), secret)
        targets[memory_scope_owner("10001", "session", session, secret)] = "session"
    unknown = memory_scope_owner("10002", "user", "30001", secret)
    targets[unknown] = "user"
    titles = resolver(targets, rows, secret)
    assert len(titles) == 6 and unknown not in titles
    assert "小明" in titles[memory_scope_owner("10001", "user", "30001", secret)]
    assert "读书群" in titles[memory_scope_owner("10001", "group", "20001", secret)]
    assert any("私聊" in title and "小明" in title for title in titles.values())
    assert any("读书群" in title and "群内名" in title for title in titles.values())


def test_newest_directory_name_wins_and_missing_name_keeps_known_number() -> None:
    """目录按最新优先提供，同一用户的较旧昵称不能覆盖；无昵称仍显示明确 QQ 号。"""
    from agent_bot.knowledge import memory_titles

    secret = "offline-scope-title-secret"
    key = memory_scope_owner("10001", "user", "30001", secret)
    rows = [{"bot_id": "10001", "group_id": "20001", "user_id": "30001",
             "group_name": None, "nickname": nickname, "card": None}
            for nickname in ("新名字", "旧名字")]
    assert "新名字" in memory_titles.scope_titles({key: "user"}, rows, secret)[key]
    rows[0]["nickname"] = None
    assert "30001" in memory_titles.scope_titles({key: "user"}, rows[:1], secret)[key]


def test_identical_member_names_and_multiple_bots_remain_distinguishable() -> None:
    """同群同名成员及不同机器人下相同用户都必须能在管理下拉框中区分。"""
    from agent_bot.knowledge.memory_titles import scope_titles

    secret = "offline-title-collisions"
    profiles = [{"bot_id": bot, "group_id": "20001", "user_id": user,
                 "group_name": "读书群", "nickname": "同名用户", "card": "同名片"}
                for bot, user in (("10001", "30001"), ("10001", "30002"),
                                  ("10002", "30001"))]
    owners = {}
    for row in profiles:
        owners[memory_scope_owner(row["bot_id"], "user", row["user_id"], secret)] = "user"
        session = external_session_id(build_session_scope(
            bot_id=row["bot_id"], user_id=row["user_id"], group_id=row["group_id"],
            group_shared=False,
        ), secret)
        owners[memory_scope_owner(row["bot_id"], "session", session, secret)] = "session"
    titles = scope_titles(owners, profiles, secret)
    assert len(set(titles.values())) == len(owners)
    for row in profiles:
        key = memory_scope_owner(row["bot_id"], "user", row["user_id"], secret)
        assert row["user_id"] in titles[key]


def test_directory_matching_releases_sqlite_lock_and_reuses_unchanged_snapshot(
    tmp_path, monkeypatch,
) -> None:
    """名称匹配不占业务写锁，同一目录重复查询复用结果，目录更名后失效。"""
    import sqlite3

    from agent_bot.database import initialize_database
    from agent_bot.integrations import tagged_memory_store
    from agent_bot.settings import DatabaseParams

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "titles.sqlite3"))
    initialize_database(params)
    store = tagged_memory_store.TaggedMemoryStore(params, secret="offline-lock-title")
    calls = []

    def resolve(owners, profiles, secret):
        """另一个写连接可立即提交，证明 CPU 匹配已归还事务连接及锁。"""
        with sqlite3.connect(params.sqlite_path, timeout=0.1) as db:
            db.execute("UPDATE chat_group_member SET nickname=nickname")
        calls.append(owners)
        return {key: "显示标题" for key in owners}

    monkeypatch.setattr(tagged_memory_store, "scope_titles", resolve)
    owners = {"a" * 64: "user"}
    assert store.admin_scope_titles(owners) == {"a" * 64: "显示标题"}
    assert store.admin_scope_titles(owners) == {"a" * 64: "显示标题"}
    assert len(calls) == 1
    with sqlite3.connect(params.sqlite_path) as db:
        db.execute("INSERT INTO chat_group_member (bot_id,group_id,user_id,nickname) "
                   "VALUES ('10001','20001','30001','新名称')")
    store.admin_scope_titles(owners)
    assert len(calls) == 2
