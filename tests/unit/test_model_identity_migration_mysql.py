"""离线MySQL方言替身执行真实约束，覆盖DDL隐式提交后重跑与配置事务。"""

import re
import sqlite3
from pathlib import Path

import pymysql
import pytest

from agent_bot.integrations.model_identity_migration import migrate_model_identity
from agent_bot.settings import DatabaseParams


class MysqlDatabase:
    """记录迁移DDL并在隔离SQLite文件模拟MySQL提交边界。"""

    def __init__(self, path):
        """准备旧结构，排序规则按原MySQL二进制列提供元数据。"""
        self.path = path
        self.events = []
        self.failure = None
        source = Path("sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8")
        source = source.replace("alias TEXT NULL", "alias TEXT NOT NULL")
        source = re.sub(
            r"CREATE UNIQUE INDEX IF NOT EXISTS uq_model_provider_upstream[^;]+;", "", source
        )
        with sqlite3.connect(path) as db:
            db.executescript(source)
            db.execute(
                "INSERT INTO agent_bot_ai_model "
                "(id,alias,upstream_model,provider_id,created_at,updated_at) "
                "VALUES(17,'legacy','Model',1,'2026-01-01','2026-01-01')"
            )
            db.execute(
                "INSERT INTO chat_setting(config_key,config_value) "
                "VALUES('chat_model','\"legacy\"')"
            )
            db.execute("INSERT INTO chat_setting_revision(id,revision) VALUES(1,3)")

    def connect(self, *_args, **_kwargs):
        """生产连接入口被替换，绝不访问网络。"""
        return MysqlConnection(self)


class MysqlConnection:
    """仅提供本迁移所需的DB-API事务与游标语义。"""

    def __init__(self, owner):
        """每次重跑读取前次DDL实际提交后的文件。"""
        self.owner = owner
        self.db = sqlite3.connect(owner.path)
        self.db.row_factory = sqlite3.Row

    def begin(self):
        """MySQL再次BEGIN时提交前次只读事务。"""
        self.db.commit()
        self.db.execute("BEGIN")

    def cursor(self):
        """只生成字典结果游标。"""
        return MysqlCursor(self)

    def commit(self):
        """提交真实文件事务。"""
        self.db.commit()

    def rollback(self):
        """不能撤销之前DDL已隐式提交的结果。"""
        self.db.rollback()

    def close(self):
        """释放文件，供后续重跑核验。"""
        self.db.close()


class MysqlCursor:
    """只适配元数据和固定方言DDL，业务查询由SQLite真实执行。"""

    def __init__(self, connection):
        """结果缓冲与当前连接独立。"""
        self.connection = connection
        self.rows = []

    def __enter__(self):
        """游标不拥有事务。"""
        return self

    def __exit__(self, *_args):
        """连接关闭负责释放数据库资源。"""

    def execute(self, sql, args=()):
        """故障注入覆盖DDL提交之后与配置事务提交之前的两个边界。"""
        db, owner = self.connection.db, self.connection.owner
        owner.events.append(sql)
        if sql.startswith(("SELECT GET_LOCK", "SELECT RELEASE_LOCK")):
            self.rows = [{"lock": 1}]
            return
        if sql.startswith("SHOW FULL COLUMNS"):
            self.rows = [
                {
                    "Field": row["name"],
                    "Null": "NO" if row["notnull"] else "YES",
                    "Collation": "utf8mb4_bin"
                    if row["name"] in ("alias", "upstream_model")
                    else None,
                }
                for row in db.execute("PRAGMA table_info(agent_bot_ai_model)")
            ]
            return
        if sql.startswith("SHOW INDEX"):
            self.rows = [
                {
                    "Key_name": index["name"],
                    "Column_name": row["name"],
                    "Seq_in_index": row["seqno"] + 1,
                    "Non_unique": int(not index["unique"]),
                    "Sub_part": None,
                }
                for index in db.execute("PRAGMA index_list(agent_bot_ai_model)")
                for row in db.execute(f'PRAGMA index_info("{index["name"]}")')
            ]
            return
        ddl = sql.startswith(("ALTER TABLE", "CREATE UNIQUE INDEX"))
        if ddl:
            db.commit()
        if sql.startswith("ALTER TABLE agent_bot_ai_model MODIFY COLUMN alias"):
            assert "VARCHAR(200) COLLATE utf8mb4_bin NULL COMMENT" in sql
            original = db.execute(
                "SELECT sql FROM sqlite_master WHERE name='agent_bot_ai_model'"
            ).fetchone()[0]
            replacement = original.replace("agent_bot_ai_model", "model_upgrade", 1).replace(
                "alias TEXT NOT NULL", "alias TEXT NULL"
            )
            db.execute(replacement)
            db.execute("INSERT INTO model_upgrade SELECT * FROM agent_bot_ai_model")
            db.execute("DROP TABLE agent_bot_ai_model")
            db.execute("ALTER TABLE model_upgrade RENAME TO agent_bot_ai_model")
            db.execute("CREATE UNIQUE INDEX uq_model_alias ON agent_bot_ai_model(alias)")
        else:
            statement = re.sub(r"\s+FOR UPDATE\b", "", sql).replace("%s", "?")
            cursor = db.execute(statement, args)
            self.rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
        if ddl:
            db.commit()
            if owner.failure == "ddl":
                owner.failure = None
                raise pymysql.OperationalError(1105, "offline after implicit commit")
        if sql.startswith("UPDATE chat_setting SET") and owner.failure == "settings":
            owner.failure = None
            raise pymysql.OperationalError(1105, "offline before settings commit")

    def fetchone(self):
        """返回一条字典结果。"""
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        """消耗全部结果。"""
        rows, self.rows = self.rows, []
        return rows


@pytest.mark.parametrize("failure", ["ddl", "settings"])
def test_mysql_implicit_ddl_commit_can_resume_without_duplicate_revision(
    tmp_path, monkeypatch, failure
):
    """部分DDL可重跑，配置失败回滚值和版本，最终稳定引用仍指向原ID。"""
    database = MysqlDatabase(tmp_path / "mysql-offline.db")
    database.failure = failure
    monkeypatch.setattr(
        "agent_bot.integrations.model_identity_migration.connect_database", database.connect
    )
    params = DatabaseParams(backend="mysql", user="offline", password="offline")
    with pytest.raises(pymysql.OperationalError):
        migrate_model_identity(params)
    with sqlite3.connect(database.path) as db:
        assert db.execute("SELECT revision FROM chat_setting_revision").fetchone() == (3,)
        assert db.execute("SELECT config_value FROM chat_setting").fetchone() == ('"legacy"',)
    migrate_model_identity(params)
    migrate_model_identity(params)
    assert migrate_model_identity(params, verify_only=True)["verified"]
    with sqlite3.connect(database.path) as db:
        assert db.execute("SELECT revision FROM chat_setting_revision").fetchone() == (4,)
        assert db.execute("SELECT config_value FROM chat_setting").fetchone() == ('"model:17"',)
        assert db.execute("SELECT id,alias FROM agent_bot_ai_model").fetchone() == (17, "legacy")
    assert sum(sql.startswith("ALTER TABLE") for sql in database.events) == 1


def test_mysql_fresh_and_upgraded_identity_constraints_match(tmp_path, monkeypatch):
    """执行新库MySQL模型DDL的方言转换，对比升级后的列、外键及唯一键。"""
    database = MysqlDatabase(tmp_path / "upgraded.db")
    monkeypatch.setattr(
        "agent_bot.integrations.model_identity_migration.connect_database", database.connect
    )
    migrate_model_identity(DatabaseParams(backend="mysql", user="offline", password="offline"))
    source = Path("sql/agent_bot_init.sql").read_text(encoding="utf-8")
    definition = re.search(
        r"CREATE TABLE IF NOT EXISTS agent_bot_ai_model \(.*?\) ENGINE=.*?;", source, re.S
    ).group()
    assert "alias VARCHAR(200) COLLATE utf8mb4_bin NULL" in definition
    assert "upstream_model VARCHAR(200) COLLATE utf8mb4_bin NOT NULL" in definition
    indexes = re.findall(r"UNIQUE KEY (\w+) \(([^)]+)\)", definition)
    statement = re.sub(r"--[^\n]*| COMMENT '[^']*'", "", definition)
    statement = re.sub(r"\) ENGINE=.*?;$", ");", statement)
    statement = re.sub(r"\s*PRIMARY KEY \(id\),", "", statement)
    statement = re.sub(r"\s*UNIQUE KEY \w+ \([^)]+\),", "", statement)
    statement = statement.replace(
        "id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT", "id INTEGER PRIMARY KEY AUTOINCREMENT", 1
    )
    statement = re.sub(r"VARCHAR\(200\) COLLATE utf8mb4_bin", "TEXT", statement)
    statement = statement.replace("BIGINT UNSIGNED", "INTEGER").replace("TINYINT", "INTEGER")
    statement = statement.replace("DATETIME(6)", "DATETIME")
    fresh = tmp_path / "fresh.db"
    with sqlite3.connect(fresh) as db:
        db.execute(statement)
        for name, fields in indexes:
            db.execute(f"CREATE UNIQUE INDEX {name} ON agent_bot_ai_model({fields})")

    def inspect(path):
        """字段类型、默认值、可空性、主键和联合唯一键必须一致。"""
        with sqlite3.connect(path) as db:
            return (
                list(db.execute("PRAGMA table_info(agent_bot_ai_model)")),
                list(db.execute("PRAGMA foreign_key_list(agent_bot_ai_model)")),
                sorted(
                    (row[1], row[2], tuple(db.execute(f'PRAGMA index_info("{row[1]}")')))
                    for row in db.execute("PRAGMA index_list(agent_bot_ai_model)")
                ),
            )

    assert inspect(database.path) == inspect(fresh)
