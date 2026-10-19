"""停服并备份后显式合并长期记忆，核验后才允许退役旧关联表。"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

LEGACY_TABLES = ("chat_memory_material", "chat_memory_tag", "chat_memory_scope")
MEMORY_COLUMNS = (
    "id", "scope_id", "memory_key", "content", "is_deleted", "create_time", "update_time",
    "scope", "tags_json", "material_ids_json",
)
NEW_FIELDS = {
    "scope": "VARCHAR(16) NULL COMMENT 'global/session/user/group'",
    "tags_json": "JSON NULL COMMENT '规范化主题标签数组'",
    "material_ids_json": "JSON NULL COMMENT '引用素材标识数组'",
}
SQLITE_MERGED_TABLE = """CREATE TABLE chat_tagged_memory_merged (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope_id TEXT NOT NULL,
    memory_key TEXT NOT NULL,
    content TEXT COLLATE NOCASE NOT NULL,
    is_deleted INTEGER NOT NULL DEFAULT 0,
    create_time DATETIME NOT NULL,
    update_time DATETIME NOT NULL,
    scope TEXT NOT NULL,
    tags_json TEXT NOT NULL CHECK (json_valid(tags_json)),
    material_ids_json TEXT NOT NULL CHECK (json_valid(material_ids_json))
)"""


class TaggedMemoryMigrationError(ValueError):
    """迁移预检的固定错误可以直接提示，不含事实内容或连接凭据。"""


def _columns(cursor: Any, backend: str) -> set[str]:
    """只读取固定事实表元数据，分辨首次迁移与已完成的重跑。"""
    if backend == "sqlite":
        cursor.execute("PRAGMA table_info(chat_tagged_memory)")
        return {row["name"] for row in cursor.fetchall()}
    cursor.execute("SHOW COLUMNS FROM chat_tagged_memory")
    return {row["Field"] for row in cursor.fetchall()}


def _tables(cursor: Any, backend: str) -> set[str]:
    """从元数据检查源表是否齐全，部分缺失时禁止猜测为空。"""
    cursor.execute(
        "SELECT name AS table_name FROM sqlite_master WHERE type='table'"
        if backend == "sqlite" else
        "SELECT TABLE_NAME AS table_name FROM information_schema.tables "
        "WHERE TABLE_SCHEMA=DATABASE()"
    )
    return {row["table_name"] for row in cursor.fetchall()}


def _source(cursor: Any) -> tuple[list[dict], dict[str, int]]:
    """拒绝孤儿和空作用域，避免合并后静默丢失关联或已有作用域授权。"""
    cursor.execute("SELECT * FROM chat_tagged_memory ORDER BY id")
    memories = list(cursor.fetchall())
    by_id = {row["id"]: row for row in memories}
    cursor.execute("SELECT scope_id,scope FROM chat_memory_scope")
    scopes = {row["scope_id"]: row["scope"] for row in cursor.fetchall()}
    used = {row["scope_id"] for row in memories}
    if used != scopes.keys():
        raise TaggedMemoryMigrationError("旧记忆存在缺失或空作用域，不能无损合并授权")
    if any(scope not in {"global", "session", "user", "group"} for scope in scopes.values()):
        raise TaggedMemoryMigrationError("旧记忆作用域类型无效")
    tags: dict[int, list[str]] = defaultdict(list)
    materials: dict[int, list[str]] = defaultdict(list)
    cursor.execute("SELECT memory_id,tag FROM chat_memory_tag ORDER BY memory_id,tag")
    tag_rows = cursor.fetchall()
    for row in tag_rows:
        if row["memory_id"] not in by_id:
            raise TaggedMemoryMigrationError("旧记忆标签存在孤儿关联")
        tags[row["memory_id"]].append(row["tag"])
    cursor.execute("SELECT memory_id,material_id FROM chat_memory_material "
                   "ORDER BY memory_id,material_id")
    material_rows = cursor.fetchall()
    if material_rows:
        cursor.execute("SELECT id FROM chat_material")
        known_materials = {row["id"] for row in cursor.fetchall()}
        for row in material_rows:
            if row["memory_id"] not in by_id or row["material_id"] not in known_materials:
                raise TaggedMemoryMigrationError("旧记忆素材存在孤儿关联")
            materials[row["memory_id"]].append(row["material_id"])
    expected = []
    for row in memories:
        item = {name: row[name] for name in MEMORY_COLUMNS[:7]}
        item.update(scope=scopes[row["scope_id"]],
                    tags_json=json.dumps(sorted(tags[row["id"]]), ensure_ascii=False),
                    material_ids_json=json.dumps(sorted(materials[row["id"]])))
        expected.append(item)
    return expected, {"memories": len(memories), "scopes": len(scopes),
                      "tags": len(tag_rows), "materials": len(material_rows)}


def _verify(cursor: Any, expected: list[dict]) -> None:
    """逐行核对所有字段和 JSON 内容，任何不一致都禁止清理来源。"""
    cursor.execute("SELECT " + ",".join(MEMORY_COLUMNS) + " FROM chat_tagged_memory ORDER BY id")
    actual = list(cursor.fetchall())
    if len(actual) != len(expected):
        raise TaggedMemoryMigrationError("合并后记忆数量不一致")
    for before, after in zip(expected, actual, strict=True):
        if any(before[name] != after[name] for name in MEMORY_COLUMNS[:8]):
            raise TaggedMemoryMigrationError("合并后记忆原文、身份或时间不一致")
        for name in ("tags_json", "material_ids_json"):
            values = json.loads(after[name]) if after[name] is not None else None
            if not isinstance(values, list) or sorted(json.loads(before[name])) != sorted(values):
                raise TaggedMemoryMigrationError("合并后记忆关联不一致")


def _check_retirement_references(cursor: Any, backend: str) -> None:
    """外部表仍引用旧关联表时保留来源，不能关闭约束强制删除。"""
    if backend == "mysql":
        cursor.execute("SELECT TABLE_NAME FROM information_schema.KEY_COLUMN_USAGE "
                       "WHERE REFERENCED_TABLE_SCHEMA=DATABASE() AND REFERENCED_TABLE_NAME "
                       "IN (%s,%s,%s)", LEGACY_TABLES)
        if cursor.fetchone() is not None:
            raise TaggedMemoryMigrationError("其他表仍引用旧记忆关联表，不能退役")
    else:
        for table in _tables(cursor, backend) - set(LEGACY_TABLES):
            cursor.execute('PRAGMA foreign_key_list("' + table.replace('"', '""') + '")')
            if any(row["table"] in LEGACY_TABLES for row in cursor.fetchall()):
                raise TaggedMemoryMigrationError("其他表仍引用旧记忆关联表，不能退役")


def verify_tagged_memory_migration(connection: Any, *, backend: str) -> dict[str, int]:
    """使用字典游标逐字段验证，保留事务给调用者管理，不修改任何数据。"""
    if backend not in {"mysql", "sqlite"}:
        raise ValueError("unsupported database backend")
    with connection.cursor() as cursor:
        expected, counts = _source(cursor)
        _verify(cursor, expected)
        return counts


def _rebuild_sqlite(connection: Any, expected: list[dict]) -> None:
    """SQLite 以事务重建加入无默认值非空字段，主键序列和外部引用保持原名。"""
    with connection.cursor() as cursor:
        cursor.execute("SELECT seq FROM sqlite_sequence WHERE name='chat_tagged_memory'")
        sequence = cursor.fetchone()
        cursor.execute(SQLITE_MERGED_TABLE)
        cursor.executemany("INSERT INTO chat_tagged_memory_merged (" + ",".join(MEMORY_COLUMNS)
                           + ") VALUES (" + ",".join("%s" for _ in MEMORY_COLUMNS) + ")",
                           [tuple(row[name] for name in MEMORY_COLUMNS) for row in expected])
        # 不先重命名旧表，避免 SQLite 把旧素材外键重定向到临时名称。
        cursor.execute("DROP TABLE chat_tagged_memory")
        cursor.execute("ALTER TABLE chat_tagged_memory_merged RENAME TO chat_tagged_memory")
        cursor.execute("CREATE UNIQUE INDEX uk_tagged_memory_scope_key "
                       "ON chat_tagged_memory(scope_id,memory_key)")
        cursor.execute("CREATE INDEX idx_tagged_memory_scope_time "
                       "ON chat_tagged_memory(scope_id,is_deleted,update_time)")
        if sequence is not None:
            cursor.execute("UPDATE sqlite_sequence SET seq=%s WHERE name='chat_tagged_memory'",
                           (sequence["seq"],))
        _verify(cursor, expected)
        cursor.execute("PRAGMA foreign_key_check")
        if cursor.fetchone() is not None:
            raise TaggedMemoryMigrationError("合并后外键检查不通过")


def migrate_tagged_memory(
    connection: Any, *, backend: str, retire_legacy: bool = False,
) -> dict[str, int]:
    """拥有字典游标连接的事务；须停服备份，默认保留三张旧表以供核验和回退。"""
    if backend not in {"mysql", "sqlite"}:
        raise ValueError("unsupported database backend")
    foreign_keys = None
    try:
        with connection.cursor() as cursor:
            present = _tables(cursor, backend).intersection(LEGACY_TABLES)
            if present != set(LEGACY_TABLES):
                raise TaggedMemoryMigrationError(
                    "三张旧记忆关联表必须齐全，已退役的数据库无需再次迁移"
                )
            expected, counts = _source(cursor)
            columns = _columns(cursor, backend)
            if retire_legacy:
                _check_retirement_references(cursor, backend)
            merged = set(NEW_FIELDS).issubset(columns)
            if merged and backend == "mysql":
                cursor.execute("SELECT COUNT(*) AS count FROM chat_tagged_memory "
                               "WHERE scope IS NULL OR tags_json IS NULL "
                               "OR material_ids_json IS NULL")
                merged = cursor.fetchone()["count"] == 0
            if merged:
                _verify(cursor, expected)
        if not merged:
            if backend == "sqlite":
                if set(NEW_FIELDS).intersection(columns):
                    raise TaggedMemoryMigrationError("SQLite 记忆合并字段不完整，请核对迁移来源")
                # 外键开关只能在事务外改变；迁移期间不允许其他写者，重建仍受写锁保护。
                connection.rollback()
                connection.autocommit(True)
                with connection.cursor() as cursor:
                    cursor.execute("PRAGMA foreign_keys")
                    foreign_keys = cursor.fetchone()["foreign_keys"]
                    cursor.execute("PRAGMA foreign_keys=OFF")
                connection.autocommit(False)
                _rebuild_sqlite(connection, expected)
            else:
                with connection.cursor() as cursor:
                    for name, definition in NEW_FIELDS.items():
                        if name not in columns:
                            cursor.execute(f"ALTER TABLE chat_tagged_memory ADD COLUMN {name} "
                                           + definition)
                    for row in expected:
                        cursor.execute("SELECT scope,tags_json,material_ids_json FROM "
                                       "chat_tagged_memory WHERE id=%s", (row["id"],))
                        current = cursor.fetchone()
                        for name in NEW_FIELDS:
                            value = current[name]
                            if value is not None and (
                                value != row[name] if name == "scope"
                                else json.loads(value) != json.loads(row[name])
                            ):
                                raise TaggedMemoryMigrationError(
                                    "未完成迁移的记忆字段与旧关联不一致"
                                )
                        cursor.execute("UPDATE chat_tagged_memory SET scope=%s,tags_json=%s,"
                                       "material_ids_json=%s WHERE id=%s",
                                       (row["scope"], row["tags_json"], row["material_ids_json"],
                                        row["id"]))
                    _verify(cursor, expected)
                connection.commit()
        with connection.cursor() as cursor:
            _verify(cursor, expected)
            if backend == "mysql":
                # MySQL DDL 独立提交；回填验证后才收紧空值约束，失败保留全部来源。
                cursor.execute("ALTER TABLE chat_tagged_memory " + ",".join(
                    f"MODIFY COLUMN {name} {definition.replace(' NULL ', ' NOT NULL ')}"
                    for name, definition in NEW_FIELDS.items()
                ))
            if retire_legacy:
                if backend == "mysql":
                    cursor.execute("DROP TABLE " + ",".join(LEGACY_TABLES))
                else:
                    for table in LEGACY_TABLES:
                        cursor.execute(f"DROP TABLE {table}")
        connection.commit()
        return counts
    except BaseException:
        connection.rollback()
        raise
    finally:
        if foreign_keys is not None:
            connection.autocommit(True)
            try:
                with connection.cursor() as cursor:
                    cursor.execute(f"PRAGMA foreign_keys={int(foreign_keys)}")
            finally:
                connection.autocommit(False)
