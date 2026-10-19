"""统一会话表的新库和增量结构一致，启动校验拒绝缺少主键的错误部署。"""

import sqlite3
from pathlib import Path

import pytest

from agent_bot.memory_store import ChatMemorySchemaError
from agent_bot.session_schema import SessionContextSchema
from agent_bot.settings import DatabaseParams


@pytest.fixture(scope="session", autouse=True)
def after_nonebot_init():
    """结构测试不启动 QQ 插件。"""


def test_new_and_upgraded_sqlite_context_schemas_match(tmp_path):
    """比较真实字段与主键，历史兼容表不参与新上下文结构。"""
    schemas = []
    for name, scripts in (
        ("fresh", ["sql/agent_bot_init.sqlite.sql"]),
        ("upgrade", ["sql/legacy/session-context-v1.sqlite.sql",
                     "sql/2026-10-19-session-context.sqlite.sql"]),
    ):
        with sqlite3.connect(tmp_path / f"{name}.db") as connection:
            for script in scripts:
                connection.executescript(Path(script).read_text("utf-8"))
            schemas.append(connection.execute("PRAGMA table_info(chat_session_context)").fetchall())
    assert schemas[0] == schemas[1]
    mysql = Path("sql/agent_bot_init.sql").read_text("utf-8")
    incremental = Path("sql/2026-10-19-session-context.sql").read_text("utf-8")
    assert incremental.strip() in mysql


def test_context_schema_requires_session_primary_key(tmp_path):
    """只有字段但缺少会话唯一性不能安全合并并发消息，启动必须明确拒绝。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bad.db"))
    with sqlite3.connect(params.sqlite_path) as connection:
        original = Path("sql/agent_bot_init.sqlite.sql").read_text("utf-8")
        connection.executescript(original.replace("session_id TEXT NOT NULL PRIMARY KEY,",
                                                 "session_id TEXT NOT NULL,"))
    with pytest.raises(ChatMemorySchemaError, match="session_id"):
        SessionContextSchema(params).validate()
