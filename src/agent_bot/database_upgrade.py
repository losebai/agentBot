"""启动前按显式清单升级所选业务库，不回放初始化或历史归档脚本。"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass
from importlib.metadata import distribution
from pathlib import Path
from typing import Any

import pymysql

from agent_bot import __version__
from agent_bot.database import database_connector
from agent_bot.settings import DatabaseParams, SettingsError
from agent_bot.sql_scripts import split_sql

VERSION_TABLE = "agent_bot_schema_version"
HISTORY_TABLE = "agent_bot_schema_migration"
METADATA_TABLES = frozenset({VERSION_TABLE, HISTORY_TABLE})
MATERIAL_CLASSIFICATION_TABLES = frozenset({"chat_material_category", "chat_material_tag"})
BILIBILI_TABLES = frozenset({"agent_bot_bilibili_account", "agent_bot_bilibili_operation"})
STICKER_TABLES = frozenset({"agent_bot_sticker"})
EVOLUTION_TABLES = frozenset({
    "agent_bot_evolution_config", "agent_bot_evolution_profile", "agent_bot_evolution_event",
    "agent_bot_evolution_scope", "agent_bot_evolution_state", "agent_bot_evolution_record",
})


class UpgradeError(SettingsError):
    """正文只含固定操作提示和受控迁移 ID，不回显数据库异常或业务值。"""


@dataclass(frozen=True)
class Migration:
    """只允许发布者明确登记的有序增量，重入能力须单独声明。"""

    version: str
    name: str
    mysql: str
    sqlite: str
    reentrant: bool = False


MIGRATIONS = (
    Migration("0.5.7", "bilibili-v1", "bilibili-init.sql", "bilibili-init.sqlite.sql", True),
    Migration("0.5.9", "comfy-plugin-v1", "2026-10-28-comfy-plugin.sql",
              "2026-10-28-comfy-plugin.sqlite.sql", True),
    Migration("0.5.9", "material-classification-v1", "material-classification.sql",
              "material-classification.sqlite.sql", True),
    Migration("0.5.10", "agent-personality-v1", "agent-personality.sql",
              "agent-personality.sqlite.sql", True),
    Migration("0.5.10", "stickers-v1", "stickers-init.sql", "stickers-init.sqlite.sql", True),
    Migration("0.6.0", "agent-evolution-v1", "agent-evolution.sql",
              "agent-evolution.sqlite.sql", True),
    Migration("0.6.0", "evolution-independent-v2", "evolution-independent.sql",
              "evolution-independent.sqlite.sql", True),
)


@dataclass(frozen=True)
class UpgradeResult:
    """向启动入口报告版本及本次实际完成的迁移，不包含连接凭据。"""

    previous_version: str | None
    current_version: str
    applied: tuple[str, ...]


def read_sql_resource(name: str) -> str:
    """生产只读取当前安装包资源，缺文件时不能回退其他版本的源码脚本。"""
    return distribution("agent-bot").locate_file("agent_bot/resources/" + name).read_text("utf-8")


def _version(value: str) -> tuple[int, ...]:
    """按数值比较三段稳定版本，拒绝无法确定顺序的版本标记。"""
    if not isinstance(value, str) or not re.fullmatch(
        r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", value
    ):
        raise UpgradeError("数据库版本标记无效，停止启动以避免错误升级。")
    return tuple(map(int, value.split(".")))


def _schema(connection: Any, backend: str) -> dict[str, set[str]]:
    """读取实际表列，未登记版本的数据库必须先核对已知升级起点。"""
    with connection.cursor() as cursor:
        if backend == "mysql":
            cursor.execute(
                "SELECT TABLE_NAME AS table_name,COLUMN_NAME AS column_name "
                "FROM information_schema.columns WHERE TABLE_SCHEMA=DATABASE()"
            )
            result: dict[str, set[str]] = {}
            for row in cursor.fetchall():
                result.setdefault(row["table_name"], set()).add(row["column_name"])
            return result
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = [row["name"] for row in cursor.fetchall()]
        result = {}
        for name in names:
            quoted = name.replace('"', '""')
            cursor.execute(f'PRAGMA table_info("{quoted}")')
            result[name] = {row["name"] for row in cursor.fetchall()}
        return result


def _expected(script: str) -> dict[str, set[str]]:
    """在内存 SQLite 解析完整初始化结构，两种后端共享相同业务列名。"""
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(script)
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            name: {row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')}
            for name in names
        }


def _validate(
    actual: dict[str, set[str]],
    expected: dict[str, set[str]],
    *,
    exclude: frozenset[str] = frozenset(),
) -> None:
    """允许历史兼容列和业务附加表，不把缺少旧数据迁移的库伪装为当前版本。"""
    missing = [
        name
        for name, columns in expected.items()
        if name not in exclude and not columns <= actual.get(name, set())
    ]
    if missing:
        raise UpgradeError(
            "数据库缺少必要的旧版升级结构，请先完成历史数据迁移；缺表或字段："
            + "、".join(sorted(missing)[:5])
        )


def _validate_model_baseline(connection: Any, backend: str) -> None:
    """0.5.5 模型身份升级改变唯一键，不能仅凭同名列接受未转换的旧库。"""
    with connection.cursor() as cursor:
        if backend == "sqlite":
            cursor.execute("PRAGMA index_list(agent_bot_ai_model)")
            rows = cursor.fetchall()
            valid = any(
                row["name"] == "uq_model_provider_upstream" and row["unique"] for row in rows
            )
            cursor.execute('PRAGMA index_info("uq_model_provider_upstream")')
            columns = tuple(
                row["name"] for row in sorted(cursor.fetchall(), key=lambda row: row["seqno"])
            )
        else:
            cursor.execute(
                "SHOW INDEX FROM agent_bot_ai_model WHERE Key_name='uq_model_provider_upstream'"
            )
            rows = cursor.fetchall()
            valid = bool(rows) and all(row["Non_unique"] == 0 for row in rows)
            columns = tuple(
                row["Column_name"] for row in sorted(rows, key=lambda row: row["Seq_in_index"])
            )
        if not valid or columns != ("provider_id", "upstream_model"):
            raise UpgradeError("数据库尚未完成旧版模型身份升级，请先执行对应历史数据迁移。")


def _execute(connection: Any, script: str) -> None:
    """逐条执行受控脚本，不能使用会隐式提交 SQLite 事务的 executescript。"""
    with connection.cursor() as cursor:
        for statement in split_sql(script):
            cursor.execute(statement)


def _personality_column(connection: Any, backend: str) -> tuple[str, bool, Any] | None:
    """读取人格列的类型、非空性和默认值，区分完整新库与已提交的中间状态。"""
    with connection.cursor() as cursor:
        if backend == "mysql":
            cursor.execute(
                "SELECT DATA_TYPE AS data_type,IS_NULLABLE AS is_nullable,"
                "COLUMN_DEFAULT AS column_default FROM information_schema.columns "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='agent_bot_agent' "
                "AND COLUMN_NAME='personality_prompt'"
            )
            row = cursor.fetchone()
            if row:
                return row["data_type"], row["is_nullable"] == "NO", row["column_default"]
            return None
        cursor.execute("PRAGMA table_info(agent_bot_agent)")
        for row in cursor.fetchall():
            if row["name"] == "personality_prompt":
                return row["type"].lower(), bool(row["notnull"]), row["dflt_value"]
    return None


def _validate_personality(connection: Any, backend: str) -> None:
    """同名列仍须符合当前后端完整初始化结构，禁止将未知结构登记为迁移完成。"""
    expected = ("mediumtext", True, None) if backend == "mysql" else ("text", True, "''")
    if _personality_column(connection, backend) != expected:
        raise UpgradeError("Agent 人格字段结构不匹配，停止启动；请核对人格增量及完整初始化结构。")


def _upgrade_personality(connection: Any, backend: str, script: str) -> None:
    """仅按人格列已提交状态恢复增量，不重放 ADD 或改写既有角色与非空人格。"""
    column = _personality_column(connection, backend)
    statements = split_sql(script)
    with connection.cursor() as cursor:
        if column is None:
            cursor.execute(statements[0])
            column = _personality_column(connection, backend)
        if backend == "mysql" and column == ("mediumtext", False, None):
            # ADD 已提交时只恢复回填和非空约束；UPDATE 仅触及本次新增列的 NULL。
            for statement in statements[1:]:
                cursor.execute(statement)
    _validate_personality(connection, backend)


def upgrade_database(
    params: DatabaseParams,
    *,
    connect: Callable[..., Any] | None = None,
    read_resource: Callable[[str], str] | None = None,
    target_version: str | None = None,
    migrations: Sequence[Migration] | None = None,
) -> UpgradeResult:
    """加载配置前完成升级；SQLite 原子提交，MySQL 命名锁覆盖隐式 DDL 提交。"""
    target = target_version or __version__
    target_key = _version(target)
    catalog = tuple(MIGRATIONS if migrations is None else migrations)
    if len({item.name for item in catalog}) != len(catalog):
        raise UpgradeError("数据库迁移清单包含重复 ID，停止启动。")
    if list(catalog) != sorted(catalog, key=lambda item: _version(item.version)):
        raise UpgradeError("数据库迁移清单未按版本排序，停止启动。")
    active = tuple(item for item in catalog if _version(item.version) <= target_key)
    reader = read_resource or read_sql_resource
    if params.backend == "sqlite" and not Path(params.sqlite_path).is_file():
        raise UpgradeError("所选 SQLite 数据库不存在，请核对路径或显式初始化新库。")
    resources = {}
    filenames = {
        "agent_bot_init.sqlite.sql",
        "startup-versioning" + (".sqlite.sql" if params.backend == "sqlite" else ".sql"),
    }
    filenames.update(item.sqlite if params.backend == "sqlite" else item.mysql for item in active)
    for filename in sorted(filenames):
        try:
            resources[filename] = reader(filename)
        except (OSError, UnicodeError):
            raise UpgradeError(f"发布包缺少可用数据库升级 SQL：{filename}") from None
    try:
        expected = _expected(resources["agent_bot_init.sqlite.sql"])
    except sqlite3.Error:
        raise UpgradeError("发布包完整初始化 SQL 无效，停止数据库升级。") from None
    connector = connect or database_connector(params)
    current_step: Migration | None = None
    try:
        with closing(
            connector(
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
                connect_timeout=10,
                read_timeout=10,
                write_timeout=10,
            )
        ) as connection:
            try:
                if params.backend == "mysql":
                    lock = (
                        "agent_bot_upgrade:"
                        + hashlib.sha256(params.database.encode()).hexdigest()[:32]
                    )
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT GET_LOCK(%s,%s) AS acquired", (lock, 10))
                        if cursor.fetchone()["acquired"] != 1:
                            raise UpgradeError("数据库升级锁不可用，停止启动；请等待其他实例完成。")
                # SQLite 的 BEGIN IMMEDIATE 是跨进程写锁；MySQL 命名锁不随 DDL 提交释放。
                connection.begin()
                actual = _schema(connection, params.backend)
                previous = None
                if VERSION_TABLE in actual:
                    with connection.cursor() as cursor:
                        cursor.execute(f"SELECT version FROM {VERSION_TABLE} WHERE id=1")
                        row = cursor.fetchone()
                        previous = row["version"] if row else None
                if previous is not None and _version(previous) > target_key:
                    raise UpgradeError(
                        "数据库版本高于当前程序版本，禁止自动降级，请使用相应新版程序。"
                    )
                if previous is None:
                    # 有版本记录的旧结构须先跑增量，不能拿新结构提前拒绝它。
                    baseline = {
                        name: columns - {"personality_prompt"} if name == "agent_bot_agent"
                        else columns for name, columns in expected.items()
                    }
                    _validate(
                        actual, baseline,
                        exclude=(METADATA_TABLES | BILIBILI_TABLES | MATERIAL_CLASSIFICATION_TABLES
                                 | STICKER_TABLES | EVOLUTION_TABLES),
                    )
                    _validate_model_baseline(connection, params.backend)
                if not METADATA_TABLES <= actual.keys():
                    _execute(
                        connection,
                        resources[
                            "startup-versioning"
                            + (".sqlite.sql" if params.backend == "sqlite" else ".sql")
                        ],
                    )
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT migration_id,target_version,checksum,status FROM {HISTORY_TABLE}"
                    )
                    history = {row["migration_id"]: row for row in cursor.fetchall()}
                if history.keys() - {item.name for item in active}:
                    raise UpgradeError(
                        "数据库含当前程序未登记的迁移，请使用匹配版本，禁止猜测升级。"
                    )
                applied = []
                for item in active:
                    script = resources[item.sqlite if params.backend == "sqlite" else item.mysql]
                    checksum = hashlib.sha256(script.encode()).hexdigest()
                    old = history.get(item.name)
                    if old and (
                        old["checksum"] != checksum or old["target_version"] != item.version
                    ):
                        raise UpgradeError(
                            f"已登记迁移校验和或版本变化：{item.name}；请恢复匹配发布资源。"
                        )
                    if old and old["status"] == "applied":
                        continue
                    if old and not item.reentrant:
                        raise UpgradeError(
                            f"迁移 {item.name} 曾中断且不可重入，停止启动，请先修复或恢复备份。"
                        )
                    current_step = item
                    with connection.cursor() as cursor:
                        if old:
                            cursor.execute(
                                f"UPDATE {HISTORY_TABLE} SET status=%s,last_error=NULL "
                                "WHERE migration_id=%s",
                                ("applying", item.name),
                            )
                        else:
                            cursor.execute(
                                f"INSERT INTO {HISTORY_TABLE} "
                                "(migration_id,target_version,checksum,status) "
                                "VALUES (%s,%s,%s,%s)",
                                (item.name, item.version, checksum, "applying"),
                            )
                    if params.backend == "mysql":
                        connection.commit()
                    if item.name == "agent-personality-v1":
                        _upgrade_personality(connection, params.backend, script)
                    else:
                        _execute(connection, script)
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"UPDATE {HISTORY_TABLE} SET status=%s,"
                            "applied_at=CURRENT_TIMESTAMP,last_error=NULL "
                            "WHERE migration_id=%s",
                            ("applied", item.name),
                        )
                    if params.backend == "mysql":
                        connection.commit()
                    current_step = None
                    applied.append(item.name)
                _validate(_schema(connection, params.backend), expected)
                if any(item.name == "agent-personality-v1" for item in active):
                    _validate_personality(connection, params.backend)
                if previous != target:
                    with connection.cursor() as cursor:
                        if previous is None:
                            cursor.execute(
                                f"INSERT INTO {VERSION_TABLE} (id,version) VALUES (1,%s)", (target,)
                            )
                        else:
                            cursor.execute(
                                f"UPDATE {VERSION_TABLE} SET version=%s,"
                                "updated_at=CURRENT_TIMESTAMP WHERE id=1",
                                (target,),
                            )
                connection.commit()
                return UpgradeResult(previous, target, tuple(applied))
            except BaseException:
                connection.rollback()
                if params.backend == "mysql" and current_step is not None:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"UPDATE {HISTORY_TABLE} SET status=%s,last_error=%s "
                            "WHERE migration_id=%s",
                            ("failed", "SQL_EXECUTION_FAILED", current_step.name),
                        )
                    connection.commit()
                # 连接由 closing 无条件关闭，MySQL 会话命名锁同时释放。
                raise
    except UpgradeError:
        raise
    except (pymysql.Error, ValueError, KeyError, sqlite3.Error):
        name = current_step.name if current_step else "结构核验"
        raise UpgradeError(f"数据库自动升级失败：{name}；版本未提升，请修复后重新启动。") from None
