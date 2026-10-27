"""用 SQLite 协议替身核对 MySQL 命名锁、隐式提交和中断记录，不连接 MySQL。"""

import re
import sqlite3
from types import SimpleNamespace

import pymysql
import pytest
from tests.unit.test_database_upgrade import legacy_database, query, resource  # noqa: F401

from agent_bot.database_upgrade import MIGRATIONS, Migration, UpgradeError, upgrade_database
from agent_bot.settings import DatabaseParams
from agent_bot.sqlite_database import SqliteConnection


def mysql_factory(params, *, lock=1, fail_once=False):
    """只翻译两份已知 MySQL 建表 SQL，其余读写由真实 SQLite 事务执行。"""
    with sqlite3.connect(":memory:") as parser:
        parser.executescript(resource("startup-versioning.sqlite.sql"))
        parser.executescript(resource("bilibili-init.sqlite.sql"))
        translations = dict(parser.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
    state = SimpleNamespace(events=[], closed=0, fail_once=fail_once)

    class Connection:
        """每次启动独占一个连接，关闭代表会话锁释放。"""

        def __init__(self):
            """保存真实事务对象。"""
            self.inner = SqliteConnection(
                params.sqlite_path, cursorclass=pymysql.cursors.DictCursor
            )

        def cursor(self):
            """读取 MySQL 元数据与持久化语句共享同一连接。"""
            return Cursor(self)

        def begin(self):
            """保持 SQLite 自己的事务隔离，不靠 mock 假装持久化。"""
            self.inner.begin()

        def commit(self):
            """提交使 DDL 中断后的状态能被下一连接观察。"""
            self.inner.commit()

        def rollback(self):
            """撤销本次未提交语句。"""
            self.inner.rollback()

        def close(self):
            """记录命名锁所属连接的释放。"""
            state.closed += 1
            self.inner.close()

    class Cursor:
        """只支持升级器实际发出的 MySQL 协议差异。"""

        def __init__(self, connection):
            """转交原始游标，不新增生产代码测试开关。"""
            self.owner = connection
            self.inner = connection.inner.cursor()
            self.rows = None

        def __enter__(self):
            """进入底层游标作用域。"""
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            """统一清理底层游标。"""
            return self.inner.__exit__(*args)

        def execute(self, sql, values=None):
            """模拟元数据、一次性故障及 MySQL DDL 隐式提交。"""
            state.events.append(sql)
            self.rows = None
            if "GET_LOCK" in sql:
                self.rows = [{"acquired": lock}]
                return
            if "information_schema.columns" in sql:
                names = [
                    row[0]
                    for row in self.owner.inner._db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ]
                self.rows = [
                    {"table_name": name, "column_name": row[1]}
                    for name in names
                    for row in self.owner.inner._db.execute(f'PRAGMA table_info("{name}")')
                ]
                return
            if sql.startswith("SHOW INDEX"):
                self.rows = [
                    {"Non_unique": 0, "Column_name": value, "Seq_in_index": index}
                    for index, value in enumerate(("provider_id", "upstream_model"), 1)
                ]
                return
            if sql.startswith("CREATE TABLE"):
                if state.fail_once and "agent_bot_bilibili_operation" in sql:
                    state.fail_once = False
                    raise pymysql.OperationalError(1205, "offline injected DDL failure")
                if "ENGINE=InnoDB" in sql:
                    name = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", sql)[1]
                    sql = translations[name].replace(
                        "CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1
                    )
                self.inner.execute(sql, values)
                self.owner.commit()
                return
            self.inner.execute(sql, values)

        def fetchall(self):
            """元数据为假，所有业务记录均读取真实库。"""
            return self.rows if self.rows is not None else self.inner.fetchall()

        def fetchone(self):
            """与 MySQL DictCursor 一致返回映射或 None。"""
            return self.rows[0] if self.rows else self.inner.fetchone()

    return lambda **kwargs: Connection(), state


def test_mysql_failed_nonreentrant_step_keeps_old_version_and_stops_replay(
    legacy_database,  # noqa: F811
):
    """DDL 已提交也不能把软件版本提升；下一启动不能盲重放非幂等语句。"""
    params = DatabaseParams(backend="mysql", database="offline")
    connect, state = mysql_factory(legacy_database)
    upgrade_database(params, connect=connect, read_resource=resource)
    bad = Migration("0.5.8", "mysql-broken", "bad.sql", "bad.sql")

    def read(name):
        """第一个 DDL 成功后，第二条语句失败。"""
        return (
            "CREATE TABLE partial_test(id INTEGER); INSERT INTO missing VALUES (1);"
            if name == "bad.sql"
            else resource(name)
        )

    with pytest.raises(UpgradeError, match="mysql-broken"):
        upgrade_database(
            params,
            connect=connect,
            read_resource=read,
            target_version="0.5.8",
            migrations=(*MIGRATIONS, bad),
        )
    assert query(legacy_database, "SELECT version FROM agent_bot_schema_version") == [("0.5.7",)]
    assert query(legacy_database, "SELECT name FROM sqlite_master WHERE name='partial_test'")
    assert query(
        legacy_database,
        "SELECT status FROM agent_bot_schema_migration WHERE migration_id='mysql-broken'",
    ) == [("failed",)]
    with pytest.raises(UpgradeError, match="不可重入"):
        upgrade_database(
            params,
            connect=connect,
            read_resource=read,
            target_version="0.5.8",
            migrations=(*MIGRATIONS, bad),
        )
    assert state.closed == 3


def test_mysql_known_idempotent_step_resumes_after_partial_ddl(legacy_database):  # noqa: F811
    """B站的已审查 CREATE IF NOT EXISTS 步骤允许从记录过的失败恢复。"""
    params = DatabaseParams(backend="mysql", database="offline")
    connect, state = mysql_factory(legacy_database, fail_once=True)
    with pytest.raises(UpgradeError, match="bilibili-v1"):
        upgrade_database(params, connect=connect, read_resource=resource)
    assert not query(legacy_database, "SELECT version FROM agent_bot_schema_version")
    result = upgrade_database(params, connect=connect, read_resource=resource)
    assert result.applied == ("bilibili-v1",)
    assert state.closed == 2
    assert sum("GET_LOCK" in event for event in state.events) == 2


def test_mysql_lock_timeout_does_not_execute_any_ddl(legacy_database):  # noqa: F811
    """未获得独占升级锁时只能失败，不能继续建版本表。"""
    connect, state = mysql_factory(legacy_database, lock=0)
    with pytest.raises(UpgradeError, match="升级锁"):
        upgrade_database(
            DatabaseParams(backend="mysql", database="offline"),
            connect=connect,
            read_resource=resource,
        )
    assert not any(event.startswith("CREATE") for event in state.events)
    assert state.closed == 1
