"""模拟 MySQL DDL 隐式提交，验证失败重跑与退役仲裁。"""

from copy import deepcopy
from datetime import datetime

import pymysql
import pytest

from agent_bot.integrations.tagged_memory_migration import (
    LEGACY_TABLES,
    MEMORY_COLUMNS,
    NEW_FIELDS,
    migrate_tagged_memory,
)


class MigrationConnection:
    """只模拟驱动事务与 DDL 提交，不连接真实 MySQL。"""

    def __init__(self, *, prepared=False, fail_update=False):
        """首次迁移和已提交结构但尚未回填的状态共用同一故障模型。"""
        self.tables = {"chat_tagged_memory", *LEGACY_TABLES}
        self.columns = set(MEMORY_COLUMNS[:7])
        self.memory = {"id": 7, "scope_id": "a" * 64, "memory_key": "fact", "content": "原文",
                       "is_deleted": 1, "create_time": datetime(2026, 10, 1),
                       "update_time": datetime(2026, 10, 2)}
        if prepared:
            self.columns.update(NEW_FIELDS)
            self.memory.update(dict.fromkeys(NEW_FIELDS))
        self.persisted = deepcopy(self.memory)
        self.fail_update = fail_update
        self.events = []
        self.rows = []

    def cursor(self):
        """固定字典游标供生产迁移函数直接调用。"""
        return self

    def __enter__(self):
        """游标上下文不改变事务边界。"""
        return self

    def __exit__(self, *_args):
        """只模拟游标离开，保留连接。"""

    def execute(self, sql, args=()):
        """DDL 立即持久，DML 必须提交；失败时保留已经存在的新增字段。"""
        self.events.append(sql)
        if "information_schema.tables" in sql:
            self.rows = [{"table_name": name} for name in self.tables]
        elif sql.startswith("SHOW COLUMNS"):
            self.rows = [{"Field": name} for name in self.columns]
        elif "information_schema.KEY_COLUMN_USAGE" in sql:
            self.rows = []
        elif sql.startswith("SELECT * FROM chat_tagged_memory"):
            self.rows = [deepcopy(self.memory)]
        elif sql.startswith("SELECT scope_id,scope"):
            self.rows = [{"scope_id": "a" * 64, "scope": "user"}]
        elif sql.startswith("SELECT memory_id,tag"):
            self.rows = [{"memory_id": 7, "tag": "主题"}]
        elif sql.startswith("SELECT memory_id,material_id"):
            self.rows = []
        elif sql.startswith("SELECT COUNT(*)"):
            self.rows = [{"count": int(any(self.memory.get(name) is None for name in NEW_FIELDS))}]
        elif sql.startswith("SELECT scope,tags_json"):
            self.rows = [{name: self.memory[name] for name in NEW_FIELDS}]
        elif sql.startswith("SELECT id,scope_id"):
            self.rows = [deepcopy(self.memory)]
        elif sql.startswith("ALTER TABLE"):
            if "ADD COLUMN" in sql:
                name = sql.split("ADD COLUMN ", 1)[1].split()[0]
                self.columns.add(name)
                self.memory[name] = None
            self.commit()
        elif sql.startswith("UPDATE chat_tagged_memory"):
            if self.fail_update:
                self.fail_update = False
                raise pymysql.OperationalError(1105, "offline injected failure")
            self.memory.update(zip(NEW_FIELDS, args[:3], strict=True))
        elif sql.startswith("DROP TABLE"):
            self.tables.difference_update(sql.removeprefix("DROP TABLE ").split(","))
            self.commit()
        else:
            raise AssertionError(sql)

    def fetchone(self):
        """读取当前语句的一行结果。"""
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        """消费结果，避免旧查询结果影响后续分支。"""
        rows, self.rows = self.rows, []
        return rows

    def commit(self):
        """保留当前行及 DDL 后状态供之后回滚使用。"""
        self.persisted = deepcopy(self.memory)

    def rollback(self):
        """撤销未提交 DML，已由 DDL 提交的列不会消失。"""
        self.memory = deepcopy(self.persisted)


def test_prepared_nullable_columns_are_backfilled_on_retry():
    """三个字段已经存在但为空时，重跑必须继续回填而非直接核验失败。"""
    connection = MigrationConnection(prepared=True)
    assert migrate_tagged_memory(connection, backend="mysql") == {
        "memories": 1, "scopes": 1, "tags": 1, "materials": 0,
    }
    assert connection.memory["scope"] == "user"
    assert connection.memory["tags_json"] == '["主题"]'
    assert connection.memory["is_deleted"] == 1


def test_ddl_committed_before_failed_backfill_can_be_retried():
    """失败回滚保留已提交 ADD，下一次仍完整恢复关联且保留事实字段。"""
    connection = MigrationConnection(fail_update=True)
    original = dict(connection.memory)
    with pytest.raises(pymysql.OperationalError):
        migrate_tagged_memory(connection, backend="mysql")
    assert set(NEW_FIELDS).issubset(connection.columns)
    assert all(connection.memory[name] is None for name in NEW_FIELDS)
    migrate_tagged_memory(connection, backend="mysql")
    assert all(connection.memory[name] == value for name, value in original.items())


def test_partial_non_null_conflict_cannot_be_overwritten():
    """重跑可以补空值，不能把已经存在的冲突授权类型静默改回旧值。"""
    connection = MigrationConnection(prepared=True)
    connection.memory["scope"] = "group"
    connection.commit()
    with pytest.raises(ValueError, match="不一致"):
        migrate_tagged_memory(connection, backend="mysql", retire_legacy=True)
    assert connection.memory["scope"] == "group"
    assert set(LEGACY_TABLES).issubset(connection.tables)


def test_mysql_retirement_uses_one_atomic_drop_statement():
    """确认全部字段后单条 DDL 退役三表，避免中间只删除部分关联表。"""
    connection = MigrationConnection(prepared=True)
    migrate_tagged_memory(connection, backend="mysql", retire_legacy=True)
    drops = [sql for sql in connection.events if sql.startswith("DROP TABLE")]
    assert drops == ["DROP TABLE " + ",".join(LEGACY_TABLES)]
    assert connection.tables == {"chat_tagged_memory"}
