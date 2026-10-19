"""隔离旧库验证长期事实合并、墓碑保留和退役前完整核对。"""

import json
import sqlite3
from pathlib import Path

import pymysql
import pytest

from agent_bot.database import connect_database
from agent_bot.settings import DatabaseParams

LEGACY_SCHEMA = """
CREATE TABLE chat_memory_scope (scope_id TEXT PRIMARY KEY, scope TEXT NOT NULL);
CREATE TABLE chat_tagged_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT, scope_id TEXT NOT NULL, memory_key TEXT NOT NULL,
    content TEXT COLLATE NOCASE NOT NULL, is_deleted INTEGER NOT NULL DEFAULT 0,
    create_time DATETIME NOT NULL, update_time DATETIME NOT NULL
);
CREATE UNIQUE INDEX uk_tagged_memory_scope_key ON chat_tagged_memory(scope_id,memory_key);
CREATE INDEX idx_tagged_memory_scope_time ON chat_tagged_memory(scope_id,is_deleted,update_time);
CREATE TABLE chat_memory_tag (memory_id INTEGER NOT NULL, tag TEXT NOT NULL,
    PRIMARY KEY(memory_id,tag));
CREATE TABLE chat_material (id TEXT PRIMARY KEY);
CREATE TABLE chat_memory_material (
    memory_id INTEGER NOT NULL REFERENCES chat_tagged_memory(id),
    material_id TEXT NOT NULL REFERENCES chat_material(id), PRIMARY KEY(memory_id,material_id)
);
"""


@pytest.fixture
def legacy_memory(tmp_path):
    """测试只操作新建临时 SQLite 文件，包含有效事实与已删除墓碑。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "legacy.db"))
    with sqlite3.connect(params.sqlite_path) as db:
        db.executescript(LEGACY_SCHEMA)
        db.execute("INSERT INTO chat_memory_scope VALUES (?,?)", ("a" * 64, "user"))
        db.executemany("INSERT INTO chat_tagged_memory VALUES (?,?,?,?,?,?,?)", [
            (7, "a" * 64, "old", "原文", 0, "2026-10-01 00:00:00", "2026-10-02 00:00:00"),
            (12, "a" * 64, "deleted", "墓碑", 1, "2026-10-03 00:00:00", "2026-10-04 00:00:00"),
        ])
        db.executemany("INSERT INTO chat_memory_tag VALUES (?,?)", [(7, "主题"), (12, "历史")])
        db.execute("INSERT INTO chat_material VALUES (?)", ("b" * 32,))
        db.execute("INSERT INTO chat_memory_material VALUES (?,?)", (7, "b" * 32))
    return params


@pytest.mark.parametrize("migration_method", ["python", "sql"])
def test_merge_preserves_every_fact_and_matches_fresh_schema(
    legacy_memory, tmp_path, migration_method,
):
    """迁移保留 ID、正文、时间和墓碑；退役前后核对新库有效结构。"""
    from agent_bot.integrations.tagged_memory_migration import (
        migrate_tagged_memory,
        verify_tagged_memory_migration,
    )

    if migration_method == "sql":
        with sqlite3.connect(legacy_memory.sqlite_path) as manual:
            manual.executescript(Path(
                "sql/2026-10-20-merge-tagged-memory.sqlite.sql"
            ).read_text(encoding="utf-8"))
        manual.close()
    connection = connect_database(legacy_memory, cursorclass=pymysql.cursors.DictCursor)
    try:
        counts = migrate_tagged_memory(connection, backend="sqlite")
        assert counts == {"memories": 2, "scopes": 1, "tags": 2, "materials": 1}
        assert verify_tagged_memory_migration(connection, backend="sqlite") == counts
        assert migrate_tagged_memory(connection, backend="sqlite") == counts
        migrate_tagged_memory(connection, backend="sqlite", retire_legacy=True)
    finally:
        connection.close()
    with (
        sqlite3.connect(legacy_memory.sqlite_path) as db,
        sqlite3.connect(tmp_path / "fresh.db") as fresh,
    ):
        fresh.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8"))
        for pragma in ("table_info", "index_list", "foreign_key_list"):
            assert db.execute(f"PRAGMA {pragma}(chat_tagged_memory)").fetchall() == (
                fresh.execute(f"PRAGMA {pragma}(chat_tagged_memory)").fetchall()
            )
        rows = db.execute("SELECT * FROM chat_tagged_memory ORDER BY id").fetchall()
        assert rows[0][:7] == (
            7, "a" * 64, "old", "原文", 0, "2026-10-01 00:00:00", "2026-10-02 00:00:00"
        )
        assert rows[1][0:5] == (12, "a" * 64, "deleted", "墓碑", 1)
        assert rows[0][7] == "user"
        assert json.loads(rows[0][8]) == ["主题"]
        assert json.loads(rows[0][9]) == ["b" * 32]
        assert not db.execute("SELECT name FROM sqlite_master WHERE name IN "
                              "('chat_memory_scope','chat_memory_tag','chat_memory_material')").fetchall()


@pytest.mark.parametrize(
    "invalid", ["orphan_tag", "empty_scope", "missing_scope", "orphan_material"]
)
def test_invalid_legacy_data_fails_before_schema_changes(legacy_memory, invalid):
    """无法无损表达的旧状态需明确报错，原表和数据保持可审查。"""
    from agent_bot.integrations.tagged_memory_migration import migrate_tagged_memory

    with sqlite3.connect(legacy_memory.sqlite_path) as db:
        if invalid == "orphan_tag":
            db.execute("INSERT INTO chat_memory_tag VALUES (99,'孤儿')")
        elif invalid == "empty_scope":
            db.execute("INSERT INTO chat_memory_scope VALUES (?, 'user')", ("c" * 64,))
        elif invalid == "missing_scope":
            db.execute("DELETE FROM chat_memory_scope")
        else:
            db.execute("DELETE FROM chat_material")
    connection = connect_database(legacy_memory, cursorclass=pymysql.cursors.DictCursor)
    try:
        with pytest.raises(ValueError):
            migrate_tagged_memory(connection, backend="sqlite", retire_legacy=True)
    finally:
        connection.close()
    with sqlite3.connect(legacy_memory.sqlite_path) as db:
        assert len(db.execute("PRAGMA table_info(chat_tagged_memory)").fetchall()) == 7
        assert db.execute("SELECT COUNT(*) FROM chat_tagged_memory").fetchone() == (2,)


@pytest.mark.parametrize("tags_json", ['[]', '{"主题":true}', '"主题"'])
def test_retirement_rejects_changed_embedded_tags(legacy_memory, tags_json):
    """旧关联与新内嵌内容不一致时禁止删除回退来源。"""
    from agent_bot.integrations.tagged_memory_migration import migrate_tagged_memory

    connection = connect_database(legacy_memory, cursorclass=pymysql.cursors.DictCursor)
    try:
        migrate_tagged_memory(connection, backend="sqlite")
        with connection.cursor() as cursor:
            cursor.execute("UPDATE chat_tagged_memory SET tags_json=%s WHERE id=7", (tags_json,))
        connection.commit()
        with pytest.raises(ValueError, match="不一致"):
            migrate_tagged_memory(connection, backend="sqlite", retire_legacy=True)
    finally:
        connection.close()
    with sqlite3.connect(legacy_memory.sqlite_path) as db:
        assert db.execute("SELECT COUNT(*) FROM chat_memory_tag").fetchone() == (2,)
