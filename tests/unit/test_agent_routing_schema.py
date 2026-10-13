"""独立内存 SQLite 核对增量升级保留数据、完整约束及初始化一致性。"""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest


def test_upgrade_preserves_bindings_and_matches_initial_schema() -> None:
    """升级若遗漏外键、默认值或改写旧绑定，此测试必须失败。"""
    with closing(sqlite3.connect(":memory:")) as fresh, closing(
        sqlite3.connect(":memory:")
    ) as upgraded:
        fresh.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8"))
        upgraded.executescript("""
            PRAGMA foreign_keys = ON;
            CREATE TABLE agent_bot_ai_model(id INTEGER PRIMARY KEY);
            CREATE TABLE agent_bot_agent(id INTEGER PRIMARY KEY,
                model_id INTEGER NOT NULL REFERENCES agent_bot_ai_model(id));
            INSERT INTO agent_bot_ai_model VALUES (1), (2), (3);
            INSERT INTO agent_bot_agent VALUES (10, 1);
        """)
        upgraded.executescript(
            Path("sql/2026-10-13-agent-model-routing.sqlite.sql").read_text(encoding="utf-8")
        )
        assert upgraded.execute(
            "SELECT model_id,vision_model_id,fallback_model_id FROM agent_bot_agent"
        ).fetchone() == (1, None, None)
        # ALTER 添加的列位置不同，因此按名称比较有效字段而非物理序号。
        names = {"vision_model_id", "fallback_model_id"}
        def columns(db: sqlite3.Connection) -> dict:
            """只比较本次升级字段的类型、可空性和默认值。"""
            return {
                row[1]: row[2:6] for row in db.execute("PRAGMA table_info(agent_bot_agent)")
                if row[1] in names
            }

        def references(db: sqlite3.Connection) -> set:
            """忽略外键编号，核对真实目标及删除、更新约束。"""
            return {
                row[2:] for row in db.execute("PRAGMA foreign_key_list(agent_bot_agent)")
                if row[3] in names
            }
        assert columns(upgraded) == columns(fresh)
        assert references(upgraded) == references(fresh)
        upgraded.execute(
            "UPDATE agent_bot_agent SET vision_model_id=2,fallback_model_id=3 WHERE id=10"
        )
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.execute("DELETE FROM agent_bot_ai_model WHERE id=3")
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.execute("UPDATE agent_bot_agent SET vision_model_id=0 WHERE id=10")
        with pytest.raises(sqlite3.IntegrityError):
            upgraded.execute("UPDATE agent_bot_agent SET fallback_model_id=999 WHERE id=10")
