"""内置 SQLite 的有限 DB-API 适配，仅支持本项目使用的 SQL 差异。"""

from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from importlib.metadata import distribution
from pathlib import Path
from typing import Any

import pymysql


def _raise_database_error(error: sqlite3.Error) -> None:
    """保持现有业务异常分类，异常正文不包含 SQL、路径或参数。"""
    if isinstance(error, sqlite3.IntegrityError):
        raise pymysql.IntegrityError(1062, "SQLite constraint violation") from error
    message = str(error).lower()
    code = (
        1146
        if "no such table" in message
        else 1054
        if ("no such column" in message or "has no column" in message)
        else 1205
        if getattr(error, "sqlite_errorcode", 0) & 255
        in {
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
        }
        else 1105
    )
    raise pymysql.OperationalError(code, "SQLite database operation failed") from error


def initialize_sqlite(path: str) -> None:
    """由首次自动配置或显式初始化调用；DDL 与索引在同一事务提交。"""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    resource = distribution("taobot").locate_file(
        "agent_bot/resources/agent_bot_init.sqlite.sql"
    )
    sql = resource.read_text(encoding="utf-8")
    connection = sqlite3.connect(target, timeout=10)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.executescript("BEGIN IMMEDIATE;\n" + sql + "\nCOMMIT;")
    except sqlite3.Error as error:
        connection.rollback()
        _raise_database_error(error)
    finally:
        connection.close()


class SqliteConnection:
    """游标和事务独占一次租用，写事务在读取仲裁状态前取得数据库写锁。"""

    def __init__(
        self,
        path: str,
        *,
        timeout: float = 10,
        cursorclass: Any = pymysql.cursors.Cursor,
        autocommit: bool = False,
    ) -> None:
        """只打开已有文件；跨线程连接由连接池保证不会同时租给两个调用者。"""
        self.cursorclass = cursorclass
        self._autocommit = autocommit
        self.open = False
        try:
            uri = Path(path).resolve().as_uri() + "?mode=rw"
            self._db = sqlite3.connect(
                uri,
                uri=True,
                timeout=timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            self._db.execute("PRAGMA foreign_keys=ON")
            self.open = True
        except sqlite3.Error as error:
            _raise_database_error(error)

    def cursor(self) -> SqliteCursor:
        """返回具备上下文管理和字典游标语义的适配器。"""
        if not self.open:
            raise pymysql.InterfaceError(0, "database connection is closed")
        return SqliteCursor(self)

    def begin(self) -> None:
        """立即取得写锁，避免读后升级锁导致版本或配额竞争。"""
        if not self._db.in_transaction:
            try:
                self._db.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                _raise_database_error(error)

    def commit(self) -> None:
        """提交当前事务，由之后的操作按需开启新事务。"""
        try:
            self._db.commit()
        except sqlite3.Error as error:
            _raise_database_error(error)

    def rollback(self) -> None:
        """失败或归还连接时撤销未提交写入。"""
        try:
            self._db.rollback()
        except sqlite3.Error as error:
            _raise_database_error(error)

    def autocommit(self, value: bool) -> None:
        """连接池归还时重置模式，切换至自动提交前提交旧事务。"""
        if value and not self._autocommit:
            self.commit()
        self._autocommit = value

    def ping(self, *, reconnect: bool = False) -> None:
        """检测存活，不重连或改换数据库。"""
        if not self.open:
            raise pymysql.InterfaceError(0, "database connection is closed")
        try:
            self._db.execute("SELECT 1")
        except sqlite3.Error as error:
            _raise_database_error(error)

    def close(self) -> None:
        """关闭前释放未提交事务，避免进程保留 SQLite 写锁。"""
        if self.open:
            self._db.close()
            self.open = False


class SqliteCursor:
    """把仓库内的 MySQL 参数和查询形态映射到 SQLite 原语。"""

    def __init__(self, connection: SqliteConnection) -> None:
        """每个游标独立保存结果，字典模式沿用已有注入约定。"""
        self.connection = connection
        self._cursor = connection._db.cursor()
        self._rows: list[dict[str, Any]] | None = None

    @property
    def rowcount(self) -> int:
        """条件更新通过真实受影响行数仲裁唯一执行者。"""
        return self._cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        """返回 SQLite 实际生成的主键。"""
        return self._cursor.lastrowid

    def __enter__(self) -> SqliteCursor:
        """游标生命周期保持现有上下文管理形式。"""
        return self

    def __exit__(self, *args: Any) -> None:
        """事务所有权属于连接，退出游标只释放游标。"""
        self._cursor.close()

    def execute(self, sql: str, args: tuple = ()) -> int:
        """参数仍由驱动绑定；锁和元数据查询使用 SQLite 原语。"""
        self._rows = None
        if not self.connection._autocommit:
            self.connection.begin()
        normalized = " ".join(sql.split()).upper()
        try:
            if "INFORMATION_SCHEMA." in normalized:
                self._rows = self._metadata(normalized, args)
                return len(self._rows)
            if normalized.startswith(("SELECT GET_LOCK(", "SELECT RELEASE_LOCK(")):
                # BEGIN IMMEDIATE 在同一连接上保护整段配置事务；提交/回滚释放锁。
                self._rows = [{"lock": 1}]
                return 1
            statement = re.sub(r"\s+FOR UPDATE\b", "", sql, flags=re.I)
            statement = re.sub(r"\bINSERT IGNORE\b", "INSERT OR IGNORE", statement, flags=re.I)
            statement = re.sub(
                r"\bON DUPLICATE KEY UPDATE\b",
                "ON CONFLICT DO UPDATE SET",
                statement,
                flags=re.I,
            )
            if "ON CONFLICT DO UPDATE SET" in statement:
                statement = re.sub(
                    r"\bVALUES\(\s*(`?\w+`?)\s*\)",
                    r"excluded.\1",
                    statement,
                    flags=re.I,
                )
            statement = statement.replace("%s", "?")
            values = tuple(
                value.isoformat(" ")
                if isinstance(value, datetime)
                else (value.isoformat() if isinstance(value, date) else value)
                for value in (args or ())
            )
            self._cursor.execute(statement, values)
            return self.rowcount
        except sqlite3.Error as error:
            _raise_database_error(error)

    def executemany(self, sql: str, args: Any) -> int:
        """批量操作仍处于同一事务；任一行失败由调用方整体回滚。"""
        count = 0
        for values in args:
            count += max(0, self.execute(sql, values))
        return count

    def _metadata(self, sql: str, args: tuple) -> list[dict[str, Any]]:
        """提供既有结构校验用的列名、可空性和索引，不模拟修改 schema。"""
        table = args[1]
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", table):
            raise ValueError("database table name is invalid")
        columns = self.connection._db.execute(f'PRAGMA table_info("{table}")').fetchall()
        if "INFORMATION_SCHEMA.COLUMNS" in sql:
            return [
                {"COLUMN_NAME": row[1], "IS_NULLABLE": "NO" if row[3] or row[5] else "YES"}
                for row in columns
            ]
        if "INFORMATION_SCHEMA.TABLE_CONSTRAINTS" in sql:
            return []
        if "INFORMATION_SCHEMA.STATISTICS" not in sql:
            raise ValueError("unsupported database metadata query")
        rows = [
            {
                "INDEX_NAME": "PRIMARY",
                "NON_UNIQUE": 0,
                "SEQ_IN_INDEX": row[5],
                "COLUMN_NAME": row[1],
                "SUB_PART": None,
            }
            for row in columns
            if row[5]
        ]
        for index in self.connection._db.execute(f'PRAGMA index_list("{table}")').fetchall():
            name = index[1]
            for column in self.connection._db.execute(
                "SELECT seqno, name FROM pragma_index_info(?)", (name,)
            ).fetchall():
                rows.append(
                    {
                        "INDEX_NAME": name,
                        "NON_UNIQUE": int(not index[2]),
                        "SEQ_IN_INDEX": column[0] + 1,
                        "COLUMN_NAME": column[1],
                        "SUB_PART": None,
                    }
                )
        return [row for row in rows if len(args) < 3 or row["INDEX_NAME"] == args[2]]

    def _row(self, row: tuple) -> Any:
        """只转换 schema 声明的时间列，不把任意用户文本猜作日期。"""
        names = [item[0] for item in self._cursor.description]
        values = list(row)
        for index, name in enumerate(names):
            if name in {
                "create_time",
                "update_time",
                "created_at",
                "updated_at",
                "expires_at",
                "started_at",
                "completed_at",
                "delivered_at",
                "run_at",
            } and isinstance(values[index], str):
                values[index] = datetime.fromisoformat(values[index])
        if issubclass(self.connection.cursorclass, pymysql.cursors.DictCursor):
            return dict(zip(names, values, strict=True))
        return tuple(values)

    def fetchone(self) -> Any:
        """消耗一行元数据或实际查询结果。"""
        if self._rows is not None:
            return self._rows.pop(0) if self._rows else None
        row = self._cursor.fetchone()
        return None if row is None else self._row(row)

    def fetchall(self) -> list[Any]:
        """读取全部剩余行，遵循选定游标形态。"""
        if self._rows is not None:
            rows, self._rows = self._rows, []
            return rows
        return [self._row(row) for row in self._cursor.fetchall()]
