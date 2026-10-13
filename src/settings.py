"""MySQL 中的配置键值表读写。"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import timedelta
from functools import wraps
from types import UnionType
from typing import Any, ParamSpec, TypeVar, get_args, get_origin

import pymysql
from pydantic import SecretStr, ValidationError
from pymysql.err import DatabaseError, InterfaceError, OperationalError

from agent_bot.config import CONFIG_DESCRIPTIONS, RuntimeConfig

_SINGLETON_ID = 1
_LOCK_NAME = "chat_setting"
_LOCK_TIMEOUT_SECONDS = 5
_MAX_KEY_LENGTH = 128
_MAX_VALUE_BYTES = 1024 * 1024
_MAX_ROWS = 256

DEFAULT_TABLE = "chat_setting"
DEFAULT_REVISION_TABLE = "chat_setting_revision"

STORED_SETTING_NAMES = frozenset(RuntimeConfig.model_fields)

SECRET_SETTING_NAMES = frozenset(
    {
        "onebot_v11_access_token",
        "chat_session_secret",
        "chat_tts_token",
        "chat_naraka_cookie",
        "chat_naraka_heybox_id",
        "chat_naraka_default_role_id",
        "onebot_v11_secret",
        "chat_auto_speak_bot_id",
    }
)

SENSITIVE_SETTING_NAMES = SECRET_SETTING_NAMES | frozenset(
    {
        "chat_system_prompt",
        "chat_auto_speak_prompt",
        "chat_auto_speak_laya_prompt",
        "chat_auto_speak_groups",
        "chat_allowed_groups",
        "chat_allowed_users",
        "chat_blocked_groups",
        "chat_blocked_users",
        "superusers",
        "nickname",
        "onebot_v11_ws_urls",
        "onebot_v11_api_roots",
    }
)

SENSITIVE_COLLECTION_SETTING_NAMES = frozenset(
    {
        "chat_auto_speak_groups",
        "chat_allowed_groups",
        "chat_allowed_users",
        "chat_blocked_groups",
        "chat_blocked_users",
        "superusers",
        "nickname",
        "onebot_v11_ws_urls",
        "onebot_v11_api_roots",
    }
)

_EXPECTED_COLUMNS: dict[str, frozenset[str]] = {
    DEFAULT_TABLE: frozenset(
        {"id", "config_key", "config_value", "description", "create_time", "update_time"}
    ),
    DEFAULT_REVISION_TABLE: frozenset({"id", "revision", "update_time"}),
}


class SettingsError(RuntimeError):
    """配置数据库错误基类。"""


class SettingsNotInitializedError(SettingsError):
    """配置数据库尚未导入初始快照。"""


class SettingsAlreadyInitializedError(SettingsError):
    """配置数据库已经含有快照。"""


class SettingsSchemaError(SettingsError):
    """配置数据库 schema 不受当前版本支持。"""


class SettingsRevisionConflictError(SettingsError):
    """配置快照在保存前已被其他进程更新。"""


class SettingsValidationError(SettingsError):
    """配置快照内容不符合当前受控模型。"""


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _database_boundary(method: Callable[_P, _R]) -> Callable[_P, _R]:
    """Prevent MySQL driver exceptions from escaping the settings API."""

    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        try:
            return method(*args, **kwargs)
        except SettingsError:
            raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise SettingsError("settings database operation failed") from exc

    return wrapped


@dataclass(frozen=True, slots=True)
class VersionedSettings:
    config: RuntimeConfig
    revision: int


def runtime_config_to_plain_values(config: RuntimeConfig) -> dict[str, Any]:
    """显式解包 SecretStr；返回值用于写入数据库或运行时注入。"""

    values: dict[str, Any] = {}
    for name in RuntimeConfig.model_fields:
        values[name] = _plain_value(getattr(config, name))
    return values


def _plain_value(value: Any) -> Any:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Mapping):
        return {str(key): _plain_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(_plain_value(item) for item in value)
    if isinstance(value, (list, tuple)):
        return [_plain_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


@dataclass(frozen=True, slots=True)
class DatabaseParams:
    """chat 配置库的 MySQL 连接参数。"""

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str


class SettingsStore:
    """保存单份完整配置快照到键值表，并以 revision 提供乐观锁。"""

    def __init__(
        self,
        params: DatabaseParams,
        *,
        table: str = DEFAULT_TABLE,
        revision_table: str = DEFAULT_REVISION_TABLE,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self._params = params
        self._table = table
        self._revision_table = revision_table
        self._connect = connect or pymysql.connect

    @property
    def table(self) -> str:
        return self._table

    @_database_boundary
    def initialize(
        self,
        config: RuntimeConfig | Mapping[str, Any],
    ) -> VersionedSettings:
        """写入 revision 1；已有快照时拒绝覆盖。"""

        validated = _validate_runtime_config(config)
        rows = _config_to_rows(validated)
        with closing(self._open()) as connection:
            self._validate_schema(connection)
            with connection.cursor() as cursor:
                _acquire_lock(cursor)
                try:
                    if self._read_revision(cursor) is not None:
                        raise SettingsAlreadyInitializedError(
                            "settings database is already initialized"
                        )
                    self._replace_rows(cursor, rows, revision=1)
                    connection.commit()
                finally:
                    _release_lock(cursor)
        loaded = self.load()
        if loaded.revision != 1:
            raise SettingsError("settings verification failed")
        return loaded

    @_database_boundary
    def load(self) -> VersionedSettings:
        """读取并验证当前配置，运行时不能使用尚未修复的数据库快照。"""

        values, revision = self.load_values()
        return VersionedSettings(
            config=_validate_runtime_config(values),
            revision=revision,
        )

    @_database_boundary
    def load_values(self) -> tuple[dict[str, Any], int]:
        """读取待修改的原始快照；调用方必须通过 save 的完整校验和版本锁写回。"""

        with closing(self._open()) as connection:
            self._validate_schema(connection)
            with connection.cursor() as cursor:
                revision = self._read_revision(cursor)
                if revision is None:
                    raise SettingsNotInitializedError("settings database is not initialized")
                values = self._read_values(cursor)
        return values, revision

    @_database_boundary
    def load_revision(self) -> int:
        """只读取当前 revision，供高频刷新检查使用。"""

        with closing(self._open()) as connection:
            with connection.cursor() as cursor:
                revision = self._read_revision(cursor)
        if revision is None:
            raise SettingsNotInitializedError("settings database is not initialized")
        return revision

    @_database_boundary
    def save(
        self,
        config: RuntimeConfig | Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> VersionedSettings:
        """整体验证并原子替换快照。"""

        validated = _validate_runtime_config(config)
        rows = _config_to_rows(validated)
        if expected_revision < 1:
            raise SettingsRevisionConflictError("settings revision conflict")
        with closing(self._open()) as connection:
            self._validate_schema(connection)
            with connection.cursor() as cursor:
                _acquire_lock(cursor)
                try:
                    current_revision = self._read_revision(cursor)
                    if current_revision is None:
                        raise SettingsNotInitializedError("settings database is not initialized")
                    if current_revision != expected_revision:
                        raise SettingsRevisionConflictError("settings revision conflict")
                    self._replace_rows(cursor, rows, revision=current_revision + 1)
                    connection.commit()
                finally:
                    _release_lock(cursor)
        loaded = self.load()
        if loaded.revision != expected_revision + 1:
            raise SettingsError("settings verification failed")
        return loaded

    def _open(self) -> Any:
        return self._connect(
            host=self._params.host,
            port=self._params.port,
            user=self._params.user,
            password=self._params.password,
            database=self._params.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )

    def _validate_schema(self, connection: Any) -> None:
        expected_pairs = (
            (self._table, _EXPECTED_COLUMNS[DEFAULT_TABLE]),
            (self._revision_table, _EXPECTED_COLUMNS[DEFAULT_REVISION_TABLE]),
        )
        for actual_name, expected_columns in expected_pairs:
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT COLUMN_NAME
                        FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                        ORDER BY ORDINAL_POSITION
                        """,
                        (self._params.database, actual_name),
                    )
                    rows = cursor.fetchall()
            except (InterfaceError, OperationalError, DatabaseError) as exc:
                raise SettingsSchemaError("settings database schema is invalid") from exc
            if not rows:
                raise SettingsSchemaError("settings database schema is invalid")
            actual_columns = {str(row["COLUMN_NAME"]).lower() for row in rows}
            if not expected_columns.issubset(actual_columns):
                raise SettingsSchemaError("settings database schema is invalid")

    def _read_revision(self, cursor: Any) -> int | None:
        cursor.execute(
            f"SELECT revision FROM `{self._revision_table}` WHERE id = %s",
            (_SINGLETON_ID,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        value = row["revision"]
        if type(value) is not int:
            raise SettingsSchemaError("settings revision row is invalid")
        return value

    def _read_values(self, cursor: Any) -> dict[str, Any]:
        cursor.execute(f"SELECT config_key, config_value FROM `{self._table}` ORDER BY config_key")
        values: dict[str, Any] = {}
        for row in cursor.fetchall():
            key = row["config_key"]
            raw = row["config_value"]
            if not isinstance(key, str) or len(key) > _MAX_KEY_LENGTH:
                raise SettingsSchemaError("settings database row is invalid")
            if raw is None:
                values[key] = None
                continue
            if not isinstance(raw, (str, bytes)) or len(raw) > _MAX_VALUE_BYTES:
                raise SettingsSchemaError("settings database row is invalid")
            try:
                raw_text = raw if isinstance(raw, str) else raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise SettingsValidationError("settings snapshot payload is invalid") from exc
            try:
                values[key] = json.loads(raw_text)
            except json.JSONDecodeError as exc:
                if not _accepts_raw_text(key):
                    raise SettingsValidationError(
                        "settings snapshot payload is invalid"
                    ) from exc
                values[key] = raw_text
        if len(values) > _MAX_ROWS:
            raise SettingsSchemaError("settings database row is invalid")
        return values

    def _replace_rows(
        self,
        cursor: Any,
        rows: list[tuple[str, str, str]],
        *,
        revision: int,
    ) -> None:
        cursor.execute(f"DELETE FROM `{self._table}`")
        if rows:
            cursor.executemany(
                f"INSERT INTO `{self._table}` "
                "(config_key, config_value, description) VALUES (%s, %s, %s)",
                rows,
            )
        cursor.execute(
            f"""
            INSERT INTO `{self._revision_table}` (id, revision)
            VALUES (%s, %s)
            ON DUPLICATE KEY UPDATE revision = VALUES(revision)
            """,
            (_SINGLETON_ID, revision),
        )


def _config_to_rows(config: RuntimeConfig) -> list[tuple[str, str, str]]:
    values = runtime_config_to_plain_values(config)
    rows: list[tuple[str, str, str]] = []
    for key, value in values.items():
        if not isinstance(key, str) or len(key) > _MAX_KEY_LENGTH:
            raise SettingsValidationError("settings snapshot contains invalid field name")
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_VALUE_BYTES:
            raise SettingsValidationError("settings snapshot payload exceeds size limit")
        description = CONFIG_DESCRIPTIONS.get(key, "")
        rows.append((key, encoded, description))
    return rows


def _accepts_raw_text(key: str) -> bool:
    """判断配置字段是否允许以未编码的原始文本作为兼容输入。"""

    field_info = RuntimeConfig.model_fields.get(key)
    if field_info is None:
        return False
    annotation = field_info.annotation
    if annotation in (str, SecretStr):
        return True
    return get_origin(annotation) is UnionType and any(
        member in (str, SecretStr) for member in get_args(annotation)
    )


def _acquire_lock(cursor: Any) -> None:
    cursor.execute("SELECT GET_LOCK(%s, %s)", (_LOCK_NAME, _LOCK_TIMEOUT_SECONDS))
    row = cursor.fetchone()
    if not row or next(iter(row.values())) != 1:
        raise SettingsError("settings database lock is unavailable")


def _release_lock(cursor: Any) -> None:
    try:
        cursor.execute("SELECT RELEASE_LOCK(%s)", (_LOCK_NAME,))
    except (InterfaceError, OperationalError, DatabaseError):
        pass


def _validate_runtime_config(
    value: RuntimeConfig | Mapping[str, Any] | object,
) -> RuntimeConfig:
    if isinstance(value, RuntimeConfig):
        raw: object = runtime_config_to_plain_values(value)
    else:
        raw = value
    if isinstance(raw, Mapping):
        # 容忍已被模型删除的字段：与 PluginConfig 的 extra="ignore" 一致。
        # 下一次 store.save() 会用 model_fields 重建快照，自然清理掉这些键。
        raw = {k: v for k, v in raw.items() if k in STORED_SETTING_NAMES}
    try:
        return RuntimeConfig.model_validate(raw)
    except ValidationError as exc:
        failures: list[str] = []
        for issue in exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = issue.get("loc", ())
            field_name = location[0] if location else "runtime_config"
            if not isinstance(field_name, str) or field_name not in STORED_SETTING_NAMES:
                field_name = "runtime_config"
            rule = issue.get("type", "validation_error")
            failure = f"{field_name}[{rule}]"
            if failure not in failures:
                failures.append(failure)
            if len(failures) == 8:
                break
        reason = ",".join(failures) or "runtime_config[validation_error]"
        raise SettingsValidationError(
            f"settings snapshot failed validation: {reason}"
        ) from exc
