"""使用隔离SQLite验证模型身份升级不会改变旧绑定或其他业务数据。"""

import json
import sqlite3
from pathlib import Path

import pytest

from agent_bot.settings import DatabaseParams


def _legacy(tmp_path):
    """从完整基线构造旧模型结构，真实外键用于检查单表升级。"""
    path = tmp_path / "models.db"
    source = Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8")
    source = source.replace("alias TEXT NULL", "alias TEXT NOT NULL")
    source = source.replace(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_model_provider_upstream "
        "ON agent_bot_ai_model (provider_id, upstream_model);",
        "",
    )
    with sqlite3.connect(path) as db:
        db.executescript(source)
        db.execute(
            "INSERT INTO agent_bot_ai_provider "
            "(id,name,protocol,base_url,credential,enabled,created_at,updated_at) "
            "VALUES (1,'供应商','openai','https://example.test','secret',1,"
            "'2026-01-01','2026-01-01')"
        )
        db.execute(
            "INSERT INTO agent_bot_ai_model "
            "(id,alias,upstream_model,provider_id,enabled,created_at,updated_at) "
            "VALUES (17,'old','Model',1,1,'2026-01-01','2026-01-01')"
        )
        db.execute(
            "INSERT INTO chat_setting(config_key,config_value) VALUES (?,?)",
            ("chat_model", json.dumps("old")),
        )
        db.execute(
            "INSERT INTO chat_setting(config_key,config_value) VALUES (?,?)",
            ("unrelated", json.dumps("old")),
        )
        db.execute("INSERT INTO chat_setting_revision(id,revision) VALUES (1,9)")
        db.execute(
            "CREATE TABLE identity_binding (id INTEGER PRIMARY KEY, model_id INTEGER "
            "REFERENCES agent_bot_ai_model(id))"
        )
        db.execute("INSERT INTO identity_binding VALUES(3,17)")
    return DatabaseParams(backend="sqlite", sqlite_path=str(path))


def test_preview_apply_repeat_and_verify_preserve_identity(tmp_path):
    """预览不写，升级只变更指定引用和一次版本，重复执行没有副作用。"""
    from agent_bot.integrations.model_identity_migration import migrate_model_identity

    params = _legacy(tmp_path)
    with sqlite3.connect(params.sqlite_path) as db:
        before = list(db.iterdump())
    assert migrate_model_identity(params, dry_run=True)["settings_changed"] == 1
    with sqlite3.connect(params.sqlite_path) as db:
        assert list(db.iterdump()) == before
    migrate_model_identity(params)
    migrate_model_identity(params)
    assert migrate_model_identity(params, verify_only=True)["verified"] is True
    with sqlite3.connect(params.sqlite_path) as db:
        assert db.execute("SELECT id,alias,upstream_model FROM agent_bot_ai_model").fetchone() == (
            17,
            "old",
            "Model",
        )
        assert dict(db.execute("SELECT config_key,config_value FROM chat_setting")) == {
            "chat_model": '"model:17"',
            "unrelated": '"old"',
        }
        assert db.execute("SELECT revision FROM chat_setting_revision").fetchone() == (10,)
        assert db.execute("SELECT * FROM identity_binding").fetchone() == (3, 17)
        assert list(db.execute("PRAGMA foreign_key_check")) == []
        db.execute(
            "INSERT INTO agent_bot_ai_model "
            "(alias,upstream_model,provider_id,created_at,updated_at) "
            "VALUES(NULL,'model',1,'2026-01-01','2026-01-01')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO agent_bot_ai_model "
                "(alias,upstream_model,provider_id,created_at,updated_at) "
                "VALUES(NULL,'Model',1,'2026-01-01','2026-01-01')"
            )


@pytest.mark.parametrize(
    "damage",
    [
        "INSERT INTO agent_bot_ai_model SELECT 18,'other',upstream_model,provider_id,enabled,"
        "revision,created_at,updated_at FROM agent_bot_ai_model",
        "UPDATE agent_bot_ai_model SET alias='model:99'",
        "UPDATE chat_setting SET config_value='\"missing\"' WHERE config_key='chat_model'",
    ],
)
def test_preflight_rejects_ambiguity_before_any_write(tmp_path, damage):
    """冲突、保留命名空间污染和不存在的配置均在DDL前失败。"""
    from agent_bot.integrations.model_identity_migration import migrate_model_identity

    params = _legacy(tmp_path)
    with sqlite3.connect(params.sqlite_path) as db:
        db.execute(damage)
        before = list(db.iterdump())
    with pytest.raises(ValueError):
        migrate_model_identity(params)
    with sqlite3.connect(params.sqlite_path) as db:
        assert list(db.iterdump()) == before


def test_fresh_and_upgraded_model_structure_match(tmp_path):
    """比较字段、外键和索引的真实结构，而不是只查SQL关键字。"""
    from agent_bot.integrations.model_identity_migration import migrate_model_identity

    params = _legacy(tmp_path)
    migrate_model_identity(params)
    fresh = tmp_path / "fresh.db"
    with sqlite3.connect(fresh) as db:
        db.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8"))

    def inspect(path):
        """忽略SQLite内部索引编号，比较有效约束。"""
        with sqlite3.connect(path) as db:
            return (
                list(db.execute("PRAGMA table_info(agent_bot_ai_model)")),
                list(db.execute("PRAGMA foreign_key_list(agent_bot_ai_model)")),
                sorted(
                    (row[1], row[2]) for row in db.execute("PRAGMA index_list(agent_bot_ai_model)")
                ),
            )

    assert inspect(params.sqlite_path) == inspect(fresh)


@pytest.mark.parametrize("value", [None, "", "model:17", "old"])
def test_optional_and_canonical_settings_preserve_exact_target(tmp_path, value):
    """可空配置和已升级引用不改写；旧引用仅转换到原ID。"""
    from agent_bot.integrations.model_identity_migration import migrate_model_identity

    params = _legacy(tmp_path)
    with sqlite3.connect(params.sqlite_path) as db:
        db.execute(
            "UPDATE chat_setting SET config_value=? WHERE config_key='chat_model'",
            (json.dumps(value),),
        )
        for key in (
            "chat_vision_model",
            "chat_image_model",
            "chat_tts_model",
            "chat_memory_embedding_model",
        ):
            db.execute(
                "INSERT INTO chat_setting(config_key,config_value) VALUES (?,?)",
                (key, json.dumps(value)),
            )
    result = migrate_model_identity(params)
    assert result["settings_changed"] == (5 if value == "old" else 0)
    with sqlite3.connect(params.sqlite_path) as db:
        assert {
            json.loads(row[0])
            for row in db.execute(
                "SELECT config_value FROM chat_setting WHERE config_key!='unrelated'"
            )
        } == {"model:17" if value == "old" else value}
        assert db.execute("SELECT revision FROM chat_setting_revision").fetchone() == (
            10 if value == "old" else 9,
        )


def test_sqlite_failure_rolls_back_schema_settings_and_sequence(tmp_path, monkeypatch):
    """DDL与配置在同一事务，配置写入故障不留下半升级结构。"""
    import agent_bot.integrations.model_identity_migration as migration

    params = _legacy(tmp_path)
    with sqlite3.connect(params.sqlite_path) as db:
        before = list(db.iterdump())
    original = migration._structure
    calls = 0

    def fail_after_ddl(cursor, sqlite):
        """结构已改但尚未提交时模拟核验故障。"""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ValueError("offline failure after DDL")
        return original(cursor, sqlite)

    monkeypatch.setattr(migration, "_structure", fail_after_ddl)
    with pytest.raises(ValueError, match="offline failure"):
        migration.migrate_model_identity(params)
    with sqlite3.connect(params.sqlite_path) as db:
        assert list(db.iterdump()) == before


def test_empty_model_table_preserves_autoincrement_high_watermark(tmp_path):
    """删除过全部模型的库仍不能在升级后复用历史记录ID。"""
    from agent_bot.integrations.model_identity_migration import migrate_model_identity

    params = _legacy(tmp_path)
    with sqlite3.connect(params.sqlite_path) as db:
        db.execute("DELETE FROM identity_binding")
        db.execute("DELETE FROM agent_bot_ai_model")
        db.execute("UPDATE chat_setting SET config_value='null'")
    migrate_model_identity(params)
    with sqlite3.connect(params.sqlite_path) as db:
        assert db.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='agent_bot_ai_model'"
        ).fetchone() == (17,)


def test_standalone_sqlite_increment_preserves_bindings_and_is_repeatable(tmp_path):
    """独立结构SQL保留原ID、外键和列定义，后续CLI完成引用转换。"""
    from agent_bot.integrations.model_identity_migration import migrate_model_identity

    params = _legacy(tmp_path)
    script = Path("sql/2026-10-20-model-identity.sqlite.sql").read_text(encoding="utf-8")
    with sqlite3.connect(params.sqlite_path) as db:
        before = list(db.execute("SELECT * FROM agent_bot_ai_model"))
        db.executescript(script)
        db.executescript(script)
        assert list(db.execute("SELECT * FROM agent_bot_ai_model")) == before
        assert db.execute("SELECT * FROM identity_binding").fetchone() == (3, 17)
        assert list(db.execute("PRAGMA foreign_key_check")) == []
        assert db.execute(
            "SELECT config_value FROM chat_setting WHERE config_key='chat_model'"
        ).fetchone() == ('"old"',)
    migrate_model_identity(params)
    assert migrate_model_identity(params, verify_only=True)["verified"] is True


def test_verify_rejects_schema_damage_without_writing(tmp_path):
    """核验发现同名错误索引时不偷偷修复现有数据库。"""
    from agent_bot.integrations.model_identity_migration import migrate_model_identity

    params = _legacy(tmp_path)
    migrate_model_identity(params)
    with sqlite3.connect(params.sqlite_path) as db:
        db.executescript(
            "DROP INDEX uq_model_provider_upstream; "
            "CREATE INDEX uq_model_provider_upstream ON agent_bot_ai_model(alias)"
        )
        before = list(db.iterdump())
    with pytest.raises(ValueError):
        migrate_model_identity(params, verify_only=True)
    with sqlite3.connect(params.sqlite_path) as db:
        assert list(db.iterdump()) == before
