"""测试用的 MySQL 内存替身，仅实现 SettingsStore 用到的 SQL 子集。

fixture ``fake_mysql`` 定义在 ``tests/conftest.py`` 中；本模块只提供类本身，
便于类型标注或在 fixture 之外手动构造实例。
"""

from __future__ import annotations

from typing import Any

from pymysql.err import DatabaseError, InterfaceError

_DEFAULT_TABLE_COLUMNS: dict[str, list[str]] = {
    "dispatch_chat_setting": [
        "id",
        "config_key",
        "config_value",
        "description",
        "create_time",
        "update_time",
    ],
    "dispatch_chat_setting_revision": ["id", "revision", "update_time"],
}


class _FakeCursor:
    def __init__(self, db: FakeMySQL) -> None:
        self._db = db
        self._rows: list[dict[str, Any]] = []

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, args: object = None) -> int:
        return self._db.run(sql, args, self)

    def executemany(self, sql: str, args: list[tuple[Any, ...]]) -> int:
        return sum(self._db.run(sql, row, self) for row in args)

    def fetchone(self) -> dict[str, Any] | None:
        return self._rows.pop(0) if self._rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        rows, self._rows = self._rows, []
        return rows


class _FakeConnection:
    def __init__(self, db: FakeMySQL) -> None:
        self._db = db
        self._closed = False

    def cursor(self) -> _FakeCursor:
        if self._closed:
            raise InterfaceError("connection closed")
        return _FakeCursor(self._db)

    @property
    def open(self) -> bool:
        """提供 PyMySQL 连接池使用的打开状态。"""
        return not self._closed

    def autocommit(self, value: bool) -> None:
        """设置事务模式，测试替身仍由显式提交保存快照。"""
        self.autocommit_mode = value

    def ping(self, reconnect: bool = True) -> None:
        """模拟连接池租用前的存活检查，不允许驱动自动重连。"""
        assert reconnect is False
        if self._closed:
            raise InterfaceError(0, "connection closed")

    def commit(self) -> None:
        self._db.commit()

    def rollback(self) -> None:
        self._db.rollback()

    def close(self) -> None:
        self._closed = True


class FakeMySQL:
    """In-memory substitute for the small subset of SQL the SettingsStore uses.

    All connections returned by ``connect`` share the same in-memory state, so a
    CLI command that opens a fresh connection per operation sees prior writes.
    """

    def __init__(self) -> None:
        self.table: list[dict[str, Any]] = []
        self.revision_row: dict[str, Any] | None = None
        self.table_columns: dict[str, list[str]] = {
            name: list(cols) for name, cols in _DEFAULT_TABLE_COLUMNS.items()
        }
        self._committed_table: list[dict[str, Any]] = []
        self._committed_revision: dict[str, Any] | None = None

    def connect(self, **_kwargs: Any) -> _FakeConnection:
        return _FakeConnection(self)

    def commit(self) -> None:
        self._committed_table = [dict(row) for row in self.table]
        self._committed_revision = (
            dict(self.revision_row) if self.revision_row is not None else None
        )

    def rollback(self) -> None:
        self.table = [dict(row) for row in self._committed_table]
        self.revision_row = (
            dict(self._committed_revision) if self._committed_revision is not None else None
        )

    def run(self, sql: str, args: object, cursor: _FakeCursor) -> int:
        upper = sql.strip().upper()
        if upper.startswith("SELECT GET_LOCK"):
            cursor._rows = [{"GET_LOCK(%s,%s)": 1}]
            return 1
        if upper.startswith("SELECT RELEASE_LOCK"):
            cursor._rows = [{"RELEASE_LOCK(%s)": 1}]
            return 1
        if "INFORMATION_SCHEMA.COLUMNS" in upper:
            assert isinstance(args, tuple | list) and len(args) == 2, args
            columns = self.table_columns.get(args[1], [])
            cursor._rows = [{"COLUMN_NAME": col} for col in columns]
            return len(columns)
        if upper.startswith("SELECT REVISION FROM"):
            cursor._rows = [] if self.revision_row is None else [dict(self.revision_row)]
            return len(cursor._rows)
        if upper.startswith("SELECT CONFIG_KEY, CONFIG_VALUE FROM"):
            cursor._rows = [dict(row) for row in sorted(self.table, key=lambda r: r["config_key"])]
            return len(cursor._rows)
        if upper.startswith("DELETE FROM"):
            count = len(self.table)
            self.table = []
            return count
        if "ON DUPLICATE KEY UPDATE" in upper and "REVISION" in upper:
            assert isinstance(args, tuple | list) and len(args) == 2, args
            self.revision_row = {"id": args[0], "revision": args[1]}
            return 1
        if upper.startswith("INSERT INTO"):
            assert isinstance(args, (tuple, list)) and len(args) == 3, args
            self.table.append(
                {
                    "config_key": args[0],
                    "config_value": args[1],
                    "description": args[2],
                }
            )
            return 1
        raise DatabaseError(f"unsupported sql: {sql}")
