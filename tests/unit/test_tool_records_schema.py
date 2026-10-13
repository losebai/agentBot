"""隔离 SQLite 验证工具负载升级不改写历史步骤，并保留有效 JSON 约束。"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


def test_tool_payload_upgrade_matches_new_database_and_preserves_steps() -> None:
    """字段默认值、类型或 JSON 约束缺失，以及历史数据变化都会使测试失败。"""
    with (
        closing(sqlite3.connect(":memory:")) as fresh,
        closing(sqlite3.connect(":memory:")) as upgraded,
    ):
        fresh.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8"))
        upgraded.executescript("""
            CREATE TABLE agent_bot_agent_step(sequence INTEGER PRIMARY KEY, summary TEXT);
            INSERT INTO agent_bot_agent_step VALUES (1, '历史摘要');
        """)
        upgraded.executescript(
            Path("sql/2026-10-13-tool-call-payloads.sqlite.sql").read_text(encoding="utf-8")
        )
        assert upgraded.execute(
            "SELECT summary,arguments,result FROM agent_bot_agent_step"
        ).fetchone() == ("历史摘要", None, None)
        names = {"arguments", "result"}

        def fields(db: sqlite3.Connection) -> dict:
            """忽略新增列的位置，只核对类型、可空性和默认值。"""
            return {
                row[1]: row[2:6]
                for row in db.execute("PRAGMA table_info(agent_bot_agent_step)")
                if row[1] in names
            }

        assert fields(upgraded) == fields(fresh)
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.execute("UPDATE agent_bot_agent_step SET arguments='invalid JSON'")
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.execute("UPDATE agent_bot_agent_step SET result='invalid JSON'")
        upgraded.execute(
            "UPDATE agent_bot_agent_step SET arguments=?,result=?", ("{}", '{"is_error":false}')
        )
