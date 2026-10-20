"""显式升级保留旧事实原文与未知时间，不在启动期间偷偷迁移。"""

import json
import sqlite3
from pathlib import Path

import pytest

from agent_bot.integrations.memory_migration import migrate_memory_lifecycle
from agent_bot.settings import DatabaseParams


def test_explicit_migration_preserves_old_fact_and_summary(tmp_path):
    """旧摘要归档，旧墓碑保留，重复升级不覆盖新事实版本。"""
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE chat_tagged_memory (
            id INTEGER PRIMARY KEY,scope_id TEXT,memory_key TEXT,content TEXT,is_deleted INTEGER,
            create_time TEXT,update_time TEXT,scope TEXT,tags_json TEXT,material_ids_json TEXT);
          CREATE TABLE chat_session_context (
            session_id TEXT PRIMARY KEY,memory_summary TEXT);
        """)
        db.execute(
            "INSERT INTO chat_tagged_memory VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                17,
                "a" * 64,
                "旧偏好",
                "喜欢清淡",
                0,
                "2025-01-01 12:00:00",
                "2025-01-02 13:00:00",
                "user",
                '["口味"]',
                "[]",
            ),
        )
        db.execute("INSERT INTO chat_session_context VALUES (?,?)", ("old-session", "旧摘要原文"))
    params = DatabaseParams(backend="sqlite", sqlite_path=str(path))
    assert migrate_memory_lifecycle(params, dry_run=True)["memories"] == 1
    with sqlite3.connect(path) as db:
        assert "revision" not in {
            row[1] for row in db.execute("PRAGMA table_info(chat_tagged_memory)")
        }
    migrate_memory_lifecycle(params)
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM chat_tagged_memory WHERE id=17").fetchone()
        assert row["content"] == "喜欢清淡"
        assert row["revision"] == 1
        assert row["created_time_basis"] == "legacy_unspecified"
        assert row["source_kind"] == "legacy_unknown"
        assert row["observed_at"] is None
        session = db.execute("SELECT * FROM chat_session_context").fetchone()
        assert session["legacy_memory_summary"] == "旧摘要原文"
        assert session["memory_summary"] == ""
        assert json.loads(session["memory_facts_json"])["entries"] == []
        db.execute("UPDATE chat_tagged_memory SET content='新的明确偏好',revision=2 WHERE id=17")
    migrate_memory_lifecycle(params)
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT content FROM chat_tagged_memory").fetchone()[0] == "新的明确偏好"


def test_fresh_sqlite_schema_contains_full_lifecycle(tmp_path):
    """新库直接使用完整初始化脚本，不依赖先跑ALTER增量。"""
    with sqlite3.connect(tmp_path / "new.db") as db:
        db.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8"))
        columns = {row[1] for row in db.execute("PRAGMA table_info(chat_tagged_memory)")}
        assert {"revision", "topic", "valid_until", "content_fingerprint"} <= columns
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {
            "chat_memory_revision",
            "chat_memory_evidence",
            "chat_memory_embedding",
            "chat_memory_operation",
        } <= tables


def _legacy_database(tmp_path):
    """构造只含已完成合表及会话迁移起点的独立数据库。"""
    path = tmp_path / "legacy-memory.db"
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE chat_tagged_memory (
            id INTEGER PRIMARY KEY,scope_id TEXT,memory_key TEXT,content TEXT,is_deleted INTEGER,
            create_time TEXT,update_time TEXT,scope TEXT,tags_json TEXT,material_ids_json TEXT);
          CREATE TABLE chat_session_context (session_id TEXT PRIMARY KEY,memory_summary TEXT);
        """)
        db.execute(
            "INSERT INTO chat_tagged_memory VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                17,
                "a" * 64,
                "旧偏好",
                "喜欢清淡",
                0,
                "2025-01-01 12:00:00",
                "2025-01-02 13:00:00",
                "user",
                '["口味"]',
                "[]",
            ),
        )
        db.execute("INSERT INTO chat_session_context VALUES (?,?)", ("old-session", "旧摘要原文"))
    return DatabaseParams(backend="sqlite", sqlite_path=str(path))


@pytest.mark.parametrize(
    "damage",
    [
        "DROP TABLE chat_memory_embedding",
        "DROP INDEX idx_memory_fingerprint",
        "DROP INDEX idx_memory_topic; CREATE INDEX idx_memory_topic ON chat_tagged_memory(content)",
        "UPDATE chat_tagged_memory SET content_fingerprint='incorrect'",
        "UPDATE chat_memory_revision SET "
        "snapshot_json=json_set(snapshot_json,'$.content','篡改正文')",
        "UPDATE chat_tagged_memory SET revision=2",
        "ALTER TABLE chat_session_context DROP COLUMN legacy_memory_summary",
    ],
)
def test_verify_only_detects_damage_without_mutating_database(tmp_path, damage):
    """只核验模式应发现实际结构或数据损坏，不能悄悄建表或修补事实。"""
    params = _legacy_database(tmp_path)
    migrate_memory_lifecycle(params)
    with sqlite3.connect(params.sqlite_path) as db:
        db.executescript(damage)
        before = list(db.iterdump())
    with pytest.raises(ValueError):
        migrate_memory_lifecycle(params, verify_only=True)
    with sqlite3.connect(params.sqlite_path) as db:
        assert list(db.iterdump()) == before


def test_repeat_migration_preserves_new_summary_and_original_archive(tmp_path):
    """重复显式升级不能把新摘要清空，也不能覆盖首次归档的原文。"""
    params = _legacy_database(tmp_path)
    migrate_memory_lifecycle(params)
    facts = '{"schema_version":1,"entries":[],"source_integrity":"complete"}'
    with sqlite3.connect(params.sqlite_path) as db:
        db.execute(
            "UPDATE chat_session_context SET memory_facts_json=?,memory_summary=?",
            (facts, "新的兼容摘要"),
        )
    migrate_memory_lifecycle(params)
    with sqlite3.connect(params.sqlite_path) as db:
        assert db.execute(
            "SELECT memory_facts_json,memory_summary,legacy_memory_summary "
            "FROM chat_session_context"
        ).fetchone() == (facts, "新的兼容摘要", "旧摘要原文")


@pytest.mark.parametrize("fresh", [False, True])
def test_json_constraints_match_for_fresh_and_migrated_sqlite(tmp_path, fresh):
    """新库和升级库均拒绝损坏向量JSON，不能依赖SQLite的JSON类型名校验。"""
    params = _legacy_database(tmp_path)
    if fresh:
        with sqlite3.connect(params.sqlite_path) as db:
            db.executescript("DROP TABLE chat_tagged_memory; DROP TABLE chat_session_context;")
            db.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8"))
    else:
        migrate_memory_lifecycle(params)
    with sqlite3.connect(params.sqlite_path) as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO chat_memory_embedding VALUES(17,1,'model','broken-json','2026-10-20')"
            )


@pytest.mark.parametrize("manual_ddl", [False, True])
def test_sqlite_initial_and_incremental_added_structure_are_equal(tmp_path, manual_ddl):
    """真实执行两条路径，比较新增字段、索引、JSON约束与外键有效结构。"""
    from agent_bot.memory.schema import MEMORY_COLUMNS

    params = _legacy_database(tmp_path)
    if manual_ddl:
        with sqlite3.connect(params.sqlite_path) as db:
            db.executescript(
                Path("sql/2026-10-20-memory-lifecycle.sqlite.sql").read_text(encoding="utf-8")
            )
    migrate_memory_lifecycle(params)
    fresh_path = tmp_path / "fresh.db"
    with sqlite3.connect(fresh_path) as db:
        db.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8"))

    def inspect(path):
        """忽略原始基线字段差异，比较这次升级负责的完整结构。"""
        with sqlite3.connect(path) as db:
            columns = {
                row[1]: row[2:]
                for row in db.execute("PRAGMA table_info(chat_tagged_memory)")
                if row[1] in MEMORY_COLUMNS
            }
            tables = {}
            for table in (
                "chat_memory_embedding",
                "chat_memory_revision",
                "chat_memory_operation",
                "chat_memory_evidence",
            ):
                tables[table] = (
                    list(db.execute(f"PRAGMA table_info({table})")),
                    list(db.execute(f"PRAGMA foreign_key_list({table})")),
                    list(
                        db.execute(
                            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                        )
                    ),
                )
            indexes = {
                row[1]: list(db.execute(f"PRAGMA index_info({row[1]})"))
                for row in db.execute("PRAGMA index_list(chat_tagged_memory)")
                if row[1].startswith("idx_memory_")
            }
            return columns, tables, indexes

    assert inspect(params.sqlite_path) == inspect(fresh_path)


def test_verify_only_rejects_invalid_vector_shape(tmp_path):
    """JSON有效并不表示是可比较的数值向量，核验应发现结构错误。"""
    params = _legacy_database(tmp_path)
    migrate_memory_lifecycle(params)
    with sqlite3.connect(params.sqlite_path) as db:
        db.execute("INSERT INTO chat_memory_embedding VALUES(17,1,'model','{}','2026-10-20')")
    with pytest.raises(ValueError, match="向量"):
        migrate_memory_lifecycle(params, verify_only=True)


def test_apply_refuses_existing_wrong_index_instead_of_reporting_success(tmp_path):
    """同名错误结构不能靠IF NOT EXISTS掩盖，也不能擅自删除已有索引修复。"""
    params = _legacy_database(tmp_path)
    migrate_memory_lifecycle(params)
    with sqlite3.connect(params.sqlite_path) as db:
        db.executescript(
            "DROP INDEX idx_memory_topic; "
            "CREATE INDEX idx_memory_topic ON chat_tagged_memory(content)"
        )
    with pytest.raises(ValueError, match="索引"):
        migrate_memory_lifecycle(params)


@pytest.mark.parametrize("fresh", [False, True])
def test_evidence_source_lookup_index_is_present_and_resumable(tmp_path, fresh):
    """来源撤回核验必须定向定位事件与会话，避免FOR UPDATE扫描其他来源。"""
    if fresh:
        params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "fresh.db"))
        with sqlite3.connect(params.sqlite_path) as db:
            db.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8"))
    else:
        params = _legacy_database(tmp_path)
        migrate_memory_lifecycle(params)
    with sqlite3.connect(params.sqlite_path) as db:
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM chat_memory_evidence "
            "WHERE source_event_id=? AND source_session_id=?",
            ("event", "session"),
        )
        assert "idx_memory_evidence_event" in str(plan.fetchall())
        db.execute("DROP INDEX idx_memory_evidence_event")
    with pytest.raises(ValueError, match="索引"):
        migrate_memory_lifecycle(params, verify_only=True)
    migrate_memory_lifecycle(params)
    assert migrate_memory_lifecycle(params, verify_only=True)["verified"] is True
