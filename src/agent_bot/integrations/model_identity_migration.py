"""显式升级模型业务唯一键和配置引用，保留原ID、旧别名及所有绑定。"""

import json
import re
from contextlib import closing

import pymysql

from agent_bot.ai.model_reference import model_reference, parse_model_reference
from agent_bot.database import connect_database
from agent_bot.settings import _acquire_lock, _release_lock

MODEL_TABLE = "agent_bot_ai_model"
REFERENCE_SETTINGS = frozenset(
    {
        "chat_model",
        "chat_vision_model",
        "chat_image_model",
        "chat_tts_model",
        "chat_memory_embedding_model",
    }
)
UNIQUE_INDEX = "uq_model_provider_upstream"


def _columns(cursor, sqlite: bool) -> dict:
    """读取实际列定义，拒绝不完整的升级起点。"""
    cursor.execute(
        f"PRAGMA table_info({MODEL_TABLE})" if sqlite else f"SHOW FULL COLUMNS FROM {MODEL_TABLE}"
    )
    columns = {row["name"] if sqlite else row["Field"]: row for row in cursor.fetchall()}
    if not {"id", "alias", "provider_id", "upstream_model"} <= columns.keys():
        raise ValueError("模型目录升级起点不完整，请先完成旧版本数据库升级")
    return columns


def _indexes(cursor, sqlite: bool) -> dict:
    """核对索引实际列顺序、完整长度和唯一性，不能只凭名称跳过升级。"""
    if sqlite:
        cursor.execute(f"PRAGMA index_list({MODEL_TABLE})")
        indexes = cursor.fetchall()
        result = {}
        for index in indexes:
            name = index["name"].replace('"', '""')
            cursor.execute(f'PRAGMA index_xinfo("{name}")')
            rows = [row for row in cursor.fetchall() if row["key"]]
            result[index["name"]] = (
                tuple(row["name"] for row in rows),
                bool(index["unique"]),
                all(row["coll"] == "BINARY" for row in rows),
                not bool(index["partial"]),
            )
        return result
    cursor.execute(f"SHOW INDEX FROM {MODEL_TABLE}")
    grouped = {}
    for row in cursor.fetchall():
        grouped.setdefault(row["Key_name"], []).append(row)
    return {
        name: (
            tuple(row["Column_name"] for row in sorted(rows, key=lambda row: row["Seq_in_index"])),
            not bool(rows[0]["Non_unique"]),
            True,
            all(row.get("Sub_part") is None for row in rows),
        )
        for name, rows in grouped.items()
    }


def _structure(cursor, sqlite: bool) -> tuple[bool, bool]:
    """返回必要结构变更，错误同名约束和非保真排序规则必须先拒绝。"""
    columns = _columns(cursor, sqlite)
    nullable = not columns["alias"]["notnull"] if sqlite else columns["alias"]["Null"] == "YES"
    indexes = _indexes(cursor, sqlite)
    if indexes.get("uq_model_alias") != (("alias",), True, True, True):
        raise ValueError("旧别名唯一索引缺失或结构错误，请先核对数据库")
    expected = (("provider_id", "upstream_model"), True, True, True)
    if UNIQUE_INDEX in indexes and indexes[UNIQUE_INDEX] != expected:
        raise ValueError(f"模型联合唯一索引结构错误：{UNIQUE_INDEX}")
    if not sqlite and any(
        columns[name].get("Collation") != "utf8mb4_bin" for name in ("alias", "upstream_model")
    ):
        raise ValueError("模型标识必须使用utf8mb4_bin排序规则以保留大小写")
    return not nullable, UNIQUE_INDEX not in indexes


def _preflight(cursor) -> tuple[list[dict], list[tuple[str, str]]]:
    """所有引用和联合冲突均在DDL前检查，不删除或选择冲突中的任一模型。"""
    # 使用数据库自身排序语义预检，包含MySQL二进制排序规则的尾空格比较规则。
    cursor.execute(
        f"SELECT GROUP_CONCAT(id) AS model_ids FROM {MODEL_TABLE} "
        "GROUP BY provider_id,upstream_model HAVING COUNT(*)>1"
    )
    conflicts = cursor.fetchall()
    if conflicts:
        ids = "; ".join(str(row["model_ids"]) for row in conflicts)
        raise ValueError(f"同供应商存在重复模型，记录ID：{ids}")
    cursor.execute(f"SELECT * FROM {MODEL_TABLE} ORDER BY id")
    models = cursor.fetchall()
    aliases, ids = {}, {}
    for row in models:
        model_id = row["id"]
        canonical = model_reference(model_id)
        ids[model_id] = row
        alias = row["alias"]
        if alias is not None:
            if parse_model_reference(alias) is not None and alias != canonical:
                raise ValueError(f"旧别名占用稳定引用命名空间，记录ID：{model_id}")
            aliases[alias] = model_id
    cursor.execute("SELECT config_key,config_value FROM chat_setting ORDER BY config_key")
    changes = []
    for row in cursor.fetchall():
        key, raw = row["config_key"], row["config_value"]
        if key not in REFERENCE_SETTINGS or raw is None:
            continue
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            value = raw
        if value is None or value == "":
            continue
        if not isinstance(value, str):
            raise ValueError(f"模型配置不是字符串引用：{key}")
        model_id = aliases.get(value)
        if model_id is None:
            model_id = parse_model_reference(value)
        if model_id not in ids:
            raise ValueError(f"模型配置引用不存在：{key}")
        reference = model_reference(model_id)
        if value != reference:
            changes.append((json.dumps(reference, ensure_ascii=False), key))
    # 配置变更必须推进现有版本，禁止制造一个新的默认配置快照。
    cursor.execute("SELECT revision FROM chat_setting_revision WHERE id=1 FOR UPDATE")
    revision = cursor.fetchone()
    if changes and (
        revision is None or type(revision["revision"]) is not int or revision["revision"] < 1
    ):
        raise ValueError("配置快照版本不存在或无效，请先核对已有运行配置")
    return models, changes


def _upgrade_sqlite(cursor, models: list[dict]) -> None:
    """只重建模型单表，复用完整原DDL并在提交前验证所有列、ID与外键。"""
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=%s", (MODEL_TABLE,))
    source = cursor.fetchone()["sql"]
    source = source.replace("-- 区分大小写的模型别名", "-- 历史别名仅供兼容，新模型为空")
    cursor.execute(
        "SELECT name,sql FROM sqlite_master WHERE tbl_name=%s "
        "AND type IN ('index','trigger') AND sql IS NOT NULL",
        (MODEL_TABLE,),
    )
    objects = cursor.fetchall()
    cursor.execute("SELECT seq FROM sqlite_sequence WHERE name=%s", (MODEL_TABLE,))
    sequence = cursor.fetchone()
    temporary = "agent_bot_ai_model_identity_upgrade"
    definition, count = re.subn(
        r"\balias\s+TEXT\s+NOT\s+NULL\b", "alias TEXT NULL", source, flags=re.I
    )
    if count != 1:
        raise ValueError("SQLite旧模型表定义不受支持，禁止猜测或丢弃原约束")
    definition = re.sub(
        r"\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:\"agent_bot_ai_model\"|`agent_bot_ai_model`|agent_bot_ai_model)",
        f"CREATE TABLE {temporary}",
        definition,
        count=1,
        flags=re.I,
    )
    cursor.execute("PRAGMA foreign_key_check")
    if cursor.fetchall():
        raise ValueError("已有外键数据损坏，升级不会自动修复")
    cursor.execute(definition)
    columns = ",".join('"' + name.replace('"', '""') + '"' for name in models[0]) if models else "*"
    if models:
        cursor.execute(f"INSERT INTO {temporary} ({columns}) SELECT {columns} FROM {MODEL_TABLE}")
    cursor.execute(f"DROP TABLE {MODEL_TABLE}")
    cursor.execute(f"ALTER TABLE {temporary} RENAME TO {MODEL_TABLE}")
    for obj in objects:
        cursor.execute(obj["sql"])
    if sequence:
        cursor.execute("DELETE FROM sqlite_sequence WHERE name=%s", (MODEL_TABLE,))
        cursor.execute(
            "INSERT INTO sqlite_sequence(name,seq) VALUES(%s,%s)", (MODEL_TABLE, sequence["seq"])
        )
    cursor.execute(f"SELECT * FROM {MODEL_TABLE} ORDER BY id")
    if cursor.fetchall() != models:
        raise ValueError("模型单表升级数据核验失败，事务已取消")
    cursor.execute("PRAGMA foreign_key_check")
    if cursor.fetchall():
        raise ValueError("模型单表升级外键核验失败，事务已取消")


def migrate_model_identity(params, *, dry_run=False, verify_only=False) -> dict:
    """只升级显式业务库；预检先于DDL，配置写入与版本推进同事务提交。"""
    if dry_run and verify_only:
        raise ValueError("预览和只核验模式不能同时使用")
    sqlite = params.backend == "sqlite"
    with closing(
        connect_database(params, cursorclass=pymysql.cursors.DictCursor, autocommit=sqlite)
    ) as connection:
        locked = False
        try:
            with connection.cursor() as cursor:
                if sqlite:
                    # SQLite修改被引用父表须在事务外关闭FK；提交前完整验证，关闭连接即释放状态。
                    cursor.execute("PRAGMA foreign_keys=OFF")
                else:
                    _acquire_lock(cursor)
                    locked = True
                connection.begin()
                alias_change, index_change = _structure(cursor, sqlite)
                models, changes = _preflight(cursor)
                report = {
                    "models": len(models),
                    "settings_changed": len(changes),
                    "schema_changed": alias_change or index_change,
                    "dry_run": dry_run,
                    "verified": verify_only,
                }
                if verify_only:
                    if alias_change or index_change or changes:
                        raise ValueError("模型身份结构或运行配置尚未完成升级")
                    if sqlite:
                        cursor.execute("PRAGMA foreign_key_check")
                        if cursor.fetchall():
                            raise ValueError("数据库存在外键损坏")
                if dry_run or verify_only:
                    connection.rollback()
                    return report
                if sqlite and alias_change:
                    _upgrade_sqlite(cursor, models)
                elif alias_change:
                    cursor.execute(
                        f"ALTER TABLE {MODEL_TABLE} MODIFY COLUMN alias VARCHAR(200) "
                        "COLLATE utf8mb4_bin NULL COMMENT '历史别名仅供兼容，新模型为空', "
                        "COMMENT='模型目录，稳定主键定义引用身份'"
                    )
                if index_change:
                    cursor.execute(
                        f"CREATE UNIQUE INDEX {UNIQUE_INDEX} ON {MODEL_TABLE} "
                        "(provider_id, upstream_model)"
                    )
                if _structure(cursor, sqlite) != (False, False):
                    raise ValueError("模型身份结构升级核验失败")
                # MySQL DDL会隐式提交，重新开始事务并预检，支持DDL中断后安全重跑。
                if not sqlite:
                    connection.begin()
                    _, changes = _preflight(cursor)
                for encoded, key in changes:
                    cursor.execute(
                        "UPDATE chat_setting SET config_value=%s WHERE config_key=%s",
                        (encoded, key),
                    )
                if changes:
                    cursor.execute(
                        "UPDATE chat_setting_revision SET revision=revision+1 WHERE id=1"
                    )
            connection.commit()
            return report
        except BaseException:
            connection.rollback()
            raise
        finally:
            if locked:
                with connection.cursor() as cursor:
                    _release_lock(cursor)
