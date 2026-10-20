"""统一上下文历史迁移只操作隔离库，验证缺失旧出站表及重复迁移。"""

import sqlite3
from pathlib import Path

import pytest

from agent_bot.session_migration import migrate_session_context
from agent_bot.settings import DatabaseParams


@pytest.fixture(scope="session", autouse=True)
def after_nonebot_init():
    """仓储迁移不启动 QQ 插件，也不读取开发环境数据库。"""


@pytest.fixture
def database(tmp_path):
    """使用正式 SQLite 初始化脚本，既有历史表只在此测试库中填充。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "context.db"))
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text("utf-8"))
        connection.executescript(Path("sql/legacy/session-context-v1.sqlite.sql").read_text("utf-8"))
        connection.execute(
            "INSERT INTO chat_memory_session VALUES (?,?,?,?,?,?,?,?)",
            ("session", "a" * 32, "已有摘要", 0, 0,
             "2020-01-01", "2019-01-01", "2019-01-01"),
        )
        connection.execute(
            "INSERT INTO chat_memory_turn "
            "(session_id,request_id,user_content,assistant_content,create_time) VALUES (?,?,?,?,?)",
            ("session", "qq-old", "用户消息", "原整段回复", "2019-01-01"),
        )
    return params


def _context(params):
    """通过正式统一仓库读取迁移结果，防止只验证生成 JSON 的表面格式。"""
    from agent_bot.session_context import SessionContextPolicy, SessionContextStore

    return SessionContextStore(params, policy=SessionContextPolicy(6, 12000, True, 4000))


def test_missing_sent_table_and_expired_legacy_session_are_imported(database):
    """已知缺失出站表的部署也可迁移，旧过期时间不再导致上下文被删除。"""
    with sqlite3.connect(database.sqlite_path) as connection:
        connection.execute("DROP TABLE chat_sent_message")
    assert migrate_session_context(database) == {
        "sessions": 1, "imported": 1, "skipped": 0, "messages": 2,
    }
    context = _context(database).context("session")
    assert context.memory == ""
    assert [message.content for message in context.messages] == ["用户消息", "原整段回复"]
    with sqlite3.connect(database.sqlite_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM chat_memory_turn").fetchone()[0] == 1
        assert connection.execute(
            "SELECT legacy_memory_summary FROM chat_session_context WHERE session_id='session'",
        ).fetchone()[0] == "已有摘要"


def test_actual_sent_parts_replace_old_combined_reply_and_pending_stays_hidden(database):
    """保留分段发送的时间与素材，未确认占位不冒充成功上下文。"""
    with sqlite3.connect(database.sqlite_path) as connection:
        connection.executemany(
            "INSERT INTO chat_sent_message "
            "(session_id,generation_id,delivery_id,request_id,message_id,content,material_ids,"
            "status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            [("session", "a" * 32, key * 64, "qq-old", key, content, "[]", status, stamp)
             for key, content, status, stamp in (
                 ("b", "第一段", "sent", "2019-01-01 00:00:01"),
                 ("c", "第二段", "sent", "2019-01-01 00:00:02"),
                 ("d", "未确认", "pending", "2019-01-01 00:00:03"),
             )],
        )
    migrate_session_context(database)
    assert [m.content for m in _context(database).context("session").messages] == [
        "用户消息", "第一段", "第二段",
    ]
    assert _context(database).pending_deliveries("session", "a" * 32) == ("d" * 64,)


def test_rerun_and_dry_run_never_replace_current_context(database):
    """统计模式不写库；正式重复执行不覆盖迁移后新增消息。"""
    assert migrate_session_context(database, dry_run=True)["imported"] == 0
    assert _context(database).context("session").generation_id is None
    migrate_session_context(database)
    _context(database).record_received("session", "新输入", request_id="qq-new")
    assert migrate_session_context(database)["skipped"] == 1
    assert [m.content for m in _context(database).context("session").messages][-1] == "新输入"


def test_old_generation_delivery_identity_survives_migration(database):
    """旧代际只迁移发送身份，不带回正文，升级恢复不能重复投递旧结果。"""
    with sqlite3.connect(database.sqlite_path) as connection:
        connection.execute(
            "INSERT INTO chat_sent_message "
            "(session_id,generation_id,delivery_id,request_id,message_id,content,material_ids,"
            "status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("session", "b" * 32, "e" * 64, "qq-before-reset", "qq-message", "旧代际已发送", "[]",
             "sent", "2018-01-01"),
        )
    migrate_session_context(database)
    store = _context(database)
    previous = store.claim_delivery(
        "session", "b" * 32, "e" * 64, "qq-before-reset", None, "旧代际已发送", (),
    )
    assert previous is not None and previous.message_id == "qq-message"
    assert "旧代际已发送" not in [message.content for message in store.context("session").messages]


def test_retire_legacy_tables_keeps_migrated_context_and_other_tables(
    database: DatabaseParams,
) -> None:
    """隔离库清理后仍能追加上下文，长期记忆、归档和队列结构保持完整。"""
    migrate_session_context(database)
    sql = Path("sql/2026-10-20-retire-legacy-session-context.sqlite.sql").read_text("utf-8")
    with sqlite3.connect(database.sqlite_path) as connection:
        before = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
        connection.executescript(sql)
        connection.executescript(sql)
        after = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'",
        )}
    assert before - after == {
        "chat_memory_session", "chat_memory_turn", "chat_memory_receipt", "chat_sent_message",
    }
    store = _context(database)
    assert [m.content for m in store.context("session").messages] == ["用户消息", "原整段回复"]
    store.record_received("session", "清理后的新输入", request_id="qq-after-retire")
    assert store.context("session").messages[-1].content == "清理后的新输入"


def test_retire_without_target_table_fails_before_removing_legacy_data(
    database: DatabaseParams,
) -> None:
    """缺少统一上下文表时清理直接报错，不能先删除旧历史。"""
    sql = Path("sql/2026-10-20-retire-legacy-session-context.sqlite.sql").read_text("utf-8")
    with sqlite3.connect(database.sqlite_path) as connection:
        connection.execute("DROP TABLE chat_session_context")
        with pytest.raises(sqlite3.OperationalError, match="chat_session_context"):
            connection.executescript(sql)
        connection.rollback()
        assert connection.execute("SELECT COUNT(*) FROM chat_memory_turn").fetchone()[0] == 1
