"""显式增量升级记忆结构与旧摘要，保留原事实且支持中断后重跑。"""

import json
import math
import re
from contextlib import closing

import pymysql

from agent_bot.database import connect_database
from agent_bot.memory.policy import IMPORTANCE, fingerprint
from agent_bot.memory.schema import (
    JSON_COLUMNS,
    MEMORY_COLUMNS,
    MEMORY_INDEXES,
    MYSQL_ADDITIONAL,
    SQLITE_ADDITIONAL,
    TABLE_COLUMNS,
    TABLE_INDEXES,
    UNIQUE_KEYS,
)

EMPTY_FACTS = '{"schema_version":1,"entries":[],"source_integrity":"unknown"}'
INDEXES = MEMORY_INDEXES


def _columns(cursor, table: str, sqlite: bool) -> set[str]:
    """仅查询固定业务表元数据，缺少升级起点时明确失败。"""
    cursor.execute(f"PRAGMA table_info({table})" if sqlite else f"SHOW COLUMNS FROM {table}")
    return {row["name"] if sqlite else row["Field"] for row in cursor.fetchall()}


def _indexes(cursor, table: str, sqlite: bool) -> dict[str, tuple[tuple[str, ...], bool]]:
    """核对索引的实际列顺序和唯一性，不把同名错误索引当作已升级。"""
    if not sqlite:
        cursor.execute(f"SHOW INDEX FROM {table}")
        entries = {}
        for row in cursor.fetchall():
            entries.setdefault(row["Key_name"], []).append(row)
        return {
            name: (
                tuple(
                    row["Column_name"]
                    for row in sorted(rows, key=lambda item: item["Seq_in_index"])
                ),
                not bool(rows[0]["Non_unique"]),
            )
            for name, rows in entries.items()
        }
    cursor.execute(f"PRAGMA table_info({table})")
    primary = sorted((row["pk"], row["name"]) for row in cursor.fetchall() if row["pk"])
    result = {"PRIMARY": (tuple(name for _, name in primary), True)} if primary else {}
    cursor.execute(f"PRAGMA index_list({table})")
    indexes = cursor.fetchall()
    for row in indexes:
        # SQLite自动索引名及手工索引均只来自数据库元数据，标识符必须引用转义。
        quoted = row["name"].replace('"', '""')
        cursor.execute(f'PRAGMA index_info("{quoted}")')
        result[row["name"]] = (
            tuple(
                item["name"] for item in sorted(cursor.fetchall(), key=lambda item: item["seqno"])
            ),
            bool(row["unique"]),
        )
    return result


def _verify_structure(cursor, sqlite: bool) -> None:
    """核对辅助表、JSON约束、唯一键、外键及授权索引，核验不执行DDL。"""
    required = {
        "chat_tagged_memory": set(MEMORY_COLUMNS),
        "chat_session_context": {"memory_facts_json", "legacy_memory_summary"},
        **{table: set(columns) for table, columns in TABLE_COLUMNS.items()},
    }
    for table, columns in required.items():
        if not columns <= _columns(cursor, table, sqlite):
            raise ValueError(f"记忆生命周期表或字段不完整：{table}")
    for table, field in JSON_COLUMNS.items():
        if sqlite:
            cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=%s", (table,))
            definition = cursor.fetchone()["sql"]
            if not re.search(rf"\bjson_valid\s*\(\s*{field}\s*\)", definition, re.I):
                raise ValueError(f"记忆JSON有效性约束不完整：{table}")
        else:
            cursor.execute(f"SHOW COLUMNS FROM {table}")
            types = {row["Field"]: row["Type"].lower() for row in cursor.fetchall()}
            if types[field] != "json":
                raise ValueError(f"记忆JSON字段类型不正确：{table}")
    for table, expected in TABLE_INDEXES.items():
        indexes = _indexes(cursor, table, sqlite)
        for name, fields in expected.items():
            if indexes.get(name) != (tuple(fields.split(",")), False):
                raise ValueError(f"记忆索引缺失或列顺序不正确：{name}")
    for table, keys in UNIQUE_KEYS.items():
        unique = {
            fields for fields, is_unique in _indexes(cursor, table, sqlite).values() if is_unique
        }
        if not set(keys) <= unique:
            raise ValueError(f"记忆版本或幂等唯一约束不完整：{table}")
        if table == "chat_memory_operation":
            continue
        if sqlite:
            cursor.execute(f"PRAGMA foreign_key_list({table})")
            linked = {(row["from"], row["table"], row["to"]) for row in cursor.fetchall()}
        else:
            cursor.execute(
                "SELECT COLUMN_NAME,REFERENCED_TABLE_NAME,REFERENCED_COLUMN_NAME "
                "FROM information_schema.KEY_COLUMN_USAGE WHERE TABLE_SCHEMA=DATABASE() "
                "AND TABLE_NAME=%s AND REFERENCED_TABLE_NAME IS NOT NULL",
                (table,),
            )
            linked = {
                (row["COLUMN_NAME"], row["REFERENCED_TABLE_NAME"], row["REFERENCED_COLUMN_NAME"])
                for row in cursor.fetchall()
            }
        if ("memory_id", "chat_tagged_memory", "id") not in linked:
            raise ValueError(f"记忆辅助表缺少事实归属外键：{table}")


def _verify_data(cursor) -> None:
    """核对指纹及当前不可变正文，拒绝通过重跑迁移掩盖已经产生的数据损坏。"""
    from agent_bot.memory.embeddings import EmbeddingError, normalized_vector

    cursor.execute("SELECT * FROM chat_tagged_memory ORDER BY id")
    for row in cursor.fetchall():
        if row["content_fingerprint"] != fingerprint(row["content"]):
            raise ValueError("记忆正文与指纹不一致，请先核对备份")
        if row["revision"] < 1 or not math.isfinite(float(row["importance"])):
            raise ValueError("记忆版本或重要性数据无效")
        cursor.execute(
            "SELECT snapshot_json FROM chat_memory_revision WHERE memory_id=%s AND revision=%s",
            (row["id"], row["revision"]),
        )
        saved = cursor.fetchone()
        if saved is None:
            raise ValueError("当前事实缺少对应不可变版本")
        try:
            snapshot = json.loads(saved["snapshot_json"])
            fields = (
                "id",
                "revision",
                "content",
                "scope_id",
                "scope",
                "topic",
                "kind",
                "status",
                "subject_type",
                "subject_id",
                "source_kind",
            )
            if (
                not isinstance(snapshot, dict)
                or any(snapshot.get(field) != row[field] for field in fields)
                or snapshot.get("key") != row["memory_key"]
                or snapshot.get("tags") != json.loads(row["tags_json"])
                or snapshot.get("material_ids") != json.loads(row["material_ids_json"])
            ):
                raise ValueError("当前事实与不可变版本快照不一致")
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("记忆版本JSON格式损坏") from error
    cursor.execute(
        "SELECT memory_facts_json,memory_summary,legacy_memory_summary FROM chat_session_context"
    )
    for row in cursor.fetchall():
        if row["memory_facts_json"] is None:
            raise ValueError("会话摘要尚未归档初始化")
        try:
            facts = json.loads(row["memory_facts_json"])
            if not isinstance(facts, dict) or not isinstance(facts.get("entries"), list):
                raise ValueError("结构化会话摘要数据无效")
        except (TypeError, json.JSONDecodeError) as error:
            raise ValueError("结构化会话摘要JSON损坏") from error
    cursor.execute("SELECT model_key,vector_json FROM chat_memory_embedding")
    dimensions = {}
    for row in cursor.fetchall():
        try:
            vector = normalized_vector(json.loads(row["vector_json"]))
        except (EmbeddingError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("记忆向量数据损坏，请核对并显式重建") from error
        model_key = row["model_key"]
        if model_key in dimensions and dimensions[model_key] != len(vector):
            raise ValueError("同一模型版本的记忆向量维度不一致")
        dimensions[model_key] = len(vector)


def migrate_memory_lifecycle(params, *, dry_run=False, verify_only=False) -> dict:
    """只处理显式选择的业务库，不自动连接备用后端或修改密钥。"""
    from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore

    sqlite = params.backend == "sqlite"
    store = TaggedMemoryStore(params, secret="migration-does-not-generate-identities")
    with closing(
        connect_database(params, cursorclass=pymysql.cursors.DictCursor, autocommit=False)
    ) as connection:
        connection.begin()
        try:
            with connection.cursor() as cursor:
                columns = _columns(cursor, "chat_tagged_memory", sqlite)
                session_columns = _columns(cursor, "chat_session_context", sqlite)
                if not {"id", "scope_id", "scope", "tags_json", "material_ids_json"} <= columns:
                    raise ValueError("请先完成0.5.4标签记忆合表迁移")
                if not {"session_id", "memory_summary"} <= session_columns:
                    raise ValueError("请先完成0.5.4统一会话迁移")
                cursor.execute("SELECT COUNT(*) AS count FROM chat_tagged_memory")
                count = cursor.fetchone()["count"]
                report = {"memories": count, "dry_run": dry_run, "verified": verify_only}
                if dry_run:
                    connection.rollback()
                    return report
                if verify_only:
                    _verify_structure(cursor, sqlite)
                    _verify_data(cursor)
                    connection.rollback()
                    return report
                for name, (kind, comment) in MEMORY_COLUMNS.items():
                    if name not in columns:
                        cursor.execute(
                            f"ALTER TABLE chat_tagged_memory ADD COLUMN {name} {kind}"
                            + (f" COMMENT '{comment}'" if not sqlite else "")
                        )
                for statement in SQLITE_ADDITIONAL if sqlite else MYSQL_ADDITIONAL:
                    cursor.execute(statement)
                for table, expected in TABLE_INDEXES.items():
                    indexes = _indexes(cursor, table, sqlite)
                    for name, fields in expected.items():
                        if name not in indexes:
                            cursor.execute(f"CREATE INDEX {name} ON {table} ({fields})")
                if "memory_facts_json" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE chat_session_context ADD COLUMN memory_facts_json "
                        + (
                            "TEXT NULL CHECK (memory_facts_json IS NULL OR "
                            "json_valid(memory_facts_json))"
                            if sqlite
                            else "JSON NULL COMMENT '结构化摘要未知为空'"
                        )
                    )
                if "legacy_memory_summary" not in session_columns:
                    cursor.execute(
                        "ALTER TABLE chat_session_context ADD COLUMN legacy_memory_summary "
                        + ("TEXT NULL" if sqlite else "MEDIUMTEXT NULL COMMENT '原摘要归档'")
                    )
                # 已有同名错误结构不能通过IF NOT EXISTS冒充成功，不擅自重建表修复约束。
                _verify_structure(cursor, sqlite)
                cursor.execute("SELECT * FROM chat_tagged_memory ORDER BY id")
                rows = cursor.fetchall()
                for row in rows:
                    cursor.execute(
                        "SELECT id FROM chat_memory_revision WHERE memory_id=%s LIMIT 1",
                        (row["id"],),
                    )
                    if cursor.fetchone():
                        continue
                    if row["revision"] != 1 or row["content_fingerprint"]:
                        raise ValueError("已有新事实缺少版本历史，禁止通过迁移覆盖")
                    tags = json.loads(row["tags_json"])
                    cursor.execute(
                        "UPDATE chat_tagged_memory SET revision=1,topic=%s,"
                        "status=%s,subject_type=%s,subject_id=%s,source_kind='legacy_unknown',"
                        "importance=%s,content_fingerprint=%s,created_time_basis='legacy_unspecified',"
                        "updated_time_basis='legacy_unspecified' WHERE id=%s",
                        (
                            tags[0] if tags else "历史资料",
                            "deleted" if row["is_deleted"] else "active",
                            "user" if row["scope"] == "user" else "unknown",
                            row["scope_id"] if row["scope"] == "user" else "",
                            IMPORTANCE["fact"],
                            fingerprint(row["content"]),
                            row["id"],
                        ),
                    )
                    updated = store._row(cursor, row["id"], deleted=True)
                    store._evidence(cursor, updated, source_kind="legacy_unknown")
                    store._snapshot(
                        cursor,
                        updated,
                        "migrate",
                        "保留旧事实原文，来源与事实时间未知",
                        "migration",
                    )
                # NULL 是尚未迁移标识；后续新摘要不得被重复升级覆盖。
                cursor.execute(
                    "UPDATE chat_session_context SET legacy_memory_summary="
                    "COALESCE(legacy_memory_summary,memory_summary),"
                    "memory_summary='',memory_facts_json=%s WHERE memory_facts_json IS NULL",
                    (EMPTY_FACTS,),
                )
            connection.commit()
            return report
        except BaseException:
            connection.rollback()
            raise
