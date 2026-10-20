"""用SQLite执行真实关系约束，模拟MySQL的元数据与DDL隐式提交边界。"""

import re
import sqlite3
from datetime import datetime
from pathlib import Path

import pymysql
import pytest

from agent_bot.integrations.memory_migration import migrate_memory_lifecycle
from agent_bot.memory.schema import MEMORY_COLUMNS, sqlite_statement
from agent_bot.settings import DatabaseParams


class MysqlDatabase:
    """保存DDL已提交结构和故障状态，任何测试均不连接网络。"""

    def __init__(self, path):
        """元数据类型按原MySQL声明记录，SQLite仅承担约束和事务执行。"""
        self.path = path
        self.types = {}
        self.events = []
        self.fail_after_add = False
        self.fail_after_backfill = False

    def connect(self, *_args, **_kwargs):
        """每次迁移使用独立连接，重跑观察上次DDL提交后的真实文件。"""
        return MysqlConnection(self)


class MysqlConnection:
    """只转换项目本次迁移SQL，未支持语句直接由SQLite报错。"""

    def __init__(self, database):
        """创建临时文件连接并强制外键校验。"""
        self.database = database
        self.db = sqlite3.connect(database.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")

    def begin(self):
        """与MySQL显式事务入口保持一致。"""
        self.db.execute("BEGIN")

    def cursor(self):
        """返回完整字典游标。"""
        return MysqlCursor(self)

    def commit(self):
        """DDL后调用时模拟MySQL隐式提交。"""
        self.db.commit()

    def rollback(self):
        """只撤销当前DML，已提交DDL不受影响。"""
        self.db.rollback()

    def close(self):
        """释放当前连接，文件供重跑继续使用。"""
        self.db.close()


class MysqlCursor:
    """业务查询仍由真实数据库执行，仅适配元数据和固定DDL方言。"""

    def __init__(self, connection):
        """持有单游标结果，避免前次查询残留。"""
        self.connection = connection
        self.rows = []

    def __enter__(self):
        """进入游标不改变事务所有者。"""
        return self

    def __exit__(self, *_args):
        """连接拥有资源释放职责。"""

    def execute(self, statement, args=()):
        """故意在DDL提交后注入错误，确保生产迁移支持部分结构重跑。"""
        database, db = self.connection.database, self.connection.db
        database.events.append(statement)
        if statement.startswith("SHOW COLUMNS FROM "):
            table = statement.split()[-1]
            self.rows = [
                {
                    "Field": row["name"],
                    "Type": database.types.get((table, row["name"]), row["type"]).lower(),
                    "Null": "NO" if row["notnull"] or row["pk"] else "YES",
                    "Default": row["dflt_value"],
                }
                for row in db.execute(f"PRAGMA table_info({table})")
            ]
            return
        if statement.startswith("SHOW INDEX FROM "):
            table = statement.split()[-1]
            rows = [
                {
                    "Key_name": "PRIMARY",
                    "Column_name": row["name"],
                    "Seq_in_index": row["pk"],
                    "Non_unique": 0,
                }
                for row in db.execute(f"PRAGMA table_info({table})")
                if row["pk"]
            ]
            for index in db.execute(f"PRAGMA index_list({table})"):
                rows.extend(
                    {
                        "Key_name": index["name"],
                        "Column_name": row["name"],
                        "Seq_in_index": row["seqno"] + 1,
                        "Non_unique": int(not index["unique"]),
                    }
                    for row in db.execute(f'PRAGMA index_info("{index["name"]}")')
                )
            self.rows = rows
            return
        if "information_schema.KEY_COLUMN_USAGE" in statement:
            self.rows = [
                {
                    "COLUMN_NAME": row["from"],
                    "REFERENCED_TABLE_NAME": row["table"],
                    "REFERENCED_COLUMN_NAME": row["to"],
                }
                for row in db.execute(f"PRAGMA foreign_key_list({args[0]})")
            ]
            return
        ddl = statement.startswith(("ALTER TABLE", "CREATE TABLE", "CREATE INDEX"))
        if statement.startswith("CREATE TABLE"):
            table = statement.split("(", 1)[0].split()[-1]
            indexes = re.findall(r"^\s*KEY (\w+) \(([^)]+)\),?$", statement, re.M)
            for field, kind in re.findall(r"^\s*(\w+) (\w+(?:\(\d+\))?)", statement, re.M):
                database.types[(table, field)] = kind
            statement = sqlite_statement(statement)
            statement = re.sub(r"--[^\n]*", "", statement)
            statement = re.sub(r" CHARACTER SET \w+| COLLATE \w+", "", statement)
            # MySQL根表的普通内嵌索引拆成等价SQLite索引，列和顺序保持原值。
            statement = re.sub(r"^\s*KEY \w+ \([^)]+\),?\n", "", statement, flags=re.M)
            statement = re.sub(r",\s*\)$", "\n)", statement)
            db.execute(statement)
            for name, fields in indexes:
                db.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({fields})")
        else:
            if statement.startswith("ALTER TABLE"):
                match = re.search(r"ALTER TABLE (\w+) ADD COLUMN (\w+) (\w+)", statement)
                database.types[(match[1], match[2])] = match[3]
                statement = re.sub(r" COMMENT '[^']*'", "", statement)
                statement = re.sub(
                    r"(\w+) JSON NULL",
                    r"\1 TEXT NULL CHECK (\1 IS NULL OR "
                    r"json_valid(\1))",
                    statement,
                )
            values = tuple(
                value.isoformat(" ") if isinstance(value, datetime) else value for value in args
            )
            cursor = db.execute(statement.replace("%s", "?"), values)
            self.rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
            if statement.startswith("UPDATE chat_tagged_memory") and database.fail_after_backfill:
                database.fail_after_backfill = False
                raise pymysql.OperationalError(1105, "offline failure before snapshot")
        if ddl:
            self.connection.commit()
            if statement.startswith("ALTER TABLE") and database.fail_after_add:
                database.fail_after_add = False
                raise pymysql.OperationalError(1105, "offline failure after implicit commit")

    def fetchone(self):
        """消费一行字典结果。"""
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        """消费当前查询的全部字典结果。"""
        rows, self.rows = self.rows, []
        return rows


def _legacy(path):
    """准备合表后的旧库，内容包含已存在的删除墓碑。"""
    with sqlite3.connect(path) as db:
        db.executescript("""
          CREATE TABLE chat_tagged_memory (
            id INTEGER PRIMARY KEY,scope_id TEXT,memory_key TEXT,content TEXT,is_deleted INTEGER,
            create_time TEXT,update_time TEXT,scope TEXT,tags_json TEXT,material_ids_json TEXT);
          CREATE TABLE chat_session_context (session_id TEXT PRIMARY KEY,memory_summary TEXT);
          INSERT INTO chat_tagged_memory VALUES (17,'scope','fact','原始事实',1,
              '2025-01-01','2025-01-02','user','["主题"]','[]');
          INSERT INTO chat_session_context VALUES ('session','原摘要');
        """)


@pytest.mark.parametrize("failure", ["ddl", "backfill"])
def test_mysql_ddl_interruption_is_restartable_without_overwriting(tmp_path, monkeypatch, failure):
    """DDL部分提交后重跑补齐结构，墓碑、正文、来源与摘要保持原始依据。"""
    path = tmp_path / "mysql-fake.db"
    _legacy(path)
    database = MysqlDatabase(path)
    monkeypatch.setattr(
        "agent_bot.integrations.memory_migration.connect_database", database.connect
    )
    params = DatabaseParams(backend="mysql", user="offline", password="offline")
    database.fail_after_add = failure == "ddl"
    database.fail_after_backfill = failure == "backfill"
    with pytest.raises(pymysql.OperationalError):
        migrate_memory_lifecycle(params)
    migrate_memory_lifecycle(params)
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT content,is_deleted,revision,created_time_basis FROM chat_tagged_memory"
        ).fetchone() == ("原始事实", 1, 1, "legacy_unspecified")
        assert db.execute("SELECT legacy_memory_summary FROM chat_session_context").fetchone() == (
            "原摘要",
        )
        assert db.execute("SELECT COUNT(*) FROM chat_memory_revision").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM chat_memory_evidence").fetchone() == (1,)
    assert (
        sum(
            sql.startswith("ALTER TABLE chat_tagged_memory ADD COLUMN revision ")
            for sql in database.events
        )
        == 1
    )
    assert migrate_memory_lifecycle(params, verify_only=True)["verified"] is True


@pytest.mark.parametrize("fresh_source", ["store", "sql"])
def test_mysql_fresh_and_upgraded_lifecycle_structure_match(tmp_path, monkeypatch, fresh_source):
    """实际执行生产MySQL DDL的方言替身，对比新库和升级库的约束与字段。"""
    from agent_bot.integrations.tagged_memory_store import SCHEMA_STATEMENTS

    old = tmp_path / "old.db"
    _legacy(old)
    database = MysqlDatabase(old)
    monkeypatch.setattr(
        "agent_bot.integrations.memory_migration.connect_database", database.connect
    )
    migrate_memory_lifecycle(DatabaseParams(backend="mysql", user="offline", password="offline"))
    fresh = tmp_path / "fresh.db"
    connection = MysqlDatabase(fresh).connect()
    statements = SCHEMA_STATEMENTS
    if fresh_source == "sql":
        source = Path("sql/agent_bot_init.sql").read_text(encoding="utf-8")
        statements = re.findall(
            r"CREATE TABLE IF NOT EXISTS chat_(?:tagged_memory|memory_\w+)\s*\(.*?;",
            source,
            re.S,
        )
        assert len(statements) == 5
    with connection.cursor() as cursor:
        for statement in statements:
            cursor.execute(statement)
    connection.close()

    def inspect(path):
        """只比较本次迁移引入字段，旧库基线类型保持原样。"""
        with sqlite3.connect(path) as db:
            root = {
                row[1]: row[2:]
                for row in db.execute("PRAGMA table_info(chat_tagged_memory)")
                if row[1] in MEMORY_COLUMNS
            }
            tables = {
                table: (
                    list(db.execute(f"PRAGMA table_info({table})")),
                    list(db.execute(f"PRAGMA foreign_key_list({table})")),
                )
                for table in (
                    "chat_memory_revision",
                    "chat_memory_embedding",
                    "chat_memory_evidence",
                    "chat_memory_operation",
                )
            }
            indexes = {
                row[1]: list(db.execute(f"PRAGMA index_info({row[1]})"))
                for row in db.execute("PRAGMA index_list(chat_tagged_memory)")
                if row[1].startswith("idx_memory_")
            }
            return root, tables, indexes

    assert inspect(old) == inspect(fresh)
