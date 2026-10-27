"""数据库、Redis 与静态配置的启动引导及 NoneBot 有效配置注入。"""

from __future__ import annotations

import os
import re
from typing import Any, Literal

from dotenv import dotenv_values
from nonebot.adapters.onebot.v11.config import Config as OneBotConfig
from nonebot.config import BaseSettings
from nonebot.drivers import Driver, WebSocketClientMixin
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from agent_bot.config import RuntimeConfig
from agent_bot.settings import (
    DatabaseParams,
    SettingsStore,
    VersionedSettings,
    runtime_config_to_plain_values,
)
from agent_bot.static_config import (
    DYNAMIC_SETTING_NAMES,
    DynamicConfigSource,
    StaticConfig,
    static_values,
)

_SAFE_NONEBOT_INIT_FIELDS = frozenset(
    {
        "driver",
        "host",
        "port",
        "log_level",
        "api_timeout",
        "command_start",
        "command_sep",
        "session_expire_timeout",
    }
)


class BootstrapConfig(BaseModel):
    """默认使用 SQLite；显式选择 MySQL 时才校验其独立连接凭据。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, hide_input_in_errors=True)

    host: str = Field(default="127.0.0.1", alias="chat_db_host")
    port: int = Field(default=3306, ge=1, le=65535, alias="chat_db_port")
    user: str = Field(default="", alias="chat_db_user")
    password: SecretStr = Field(default=SecretStr(""), alias="chat_db_password", repr=False)
    database: str = Field(default="chat", alias="chat_db_name")
    backend: Literal["mysql", "sqlite"] = Field(default="sqlite", alias="chat_db_backend")
    sqlite_path: str = Field(default="data/agent_bot.sqlite3", min_length=1,
                             alias="chat_db_sqlite_path")

    @model_validator(mode="after")
    def validate_mysql_credentials(self) -> BootstrapConfig:
        """MySQL 必须明确提供连接账户；SQLite 不读取这些凭据。"""
        if self.backend == "mysql" and not self.user:
            raise ValueError("chat_db_user is required for mysql")
        if self.backend == "mysql" and "password" not in self.model_fields_set:
            raise ValueError("chat_db_password is required for mysql")
        return self


class QueueBootstrapConfig(BaseModel):
    """默认持久化到本地队列库；Redis 仅在显式选择时启用。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True, hide_input_in_errors=True)

    backend: Literal["sqlite", "redis"] = Field(default="sqlite", alias="chat_queue_backend")
    sqlite_path: str = Field(default="data/queue.sqlite3", min_length=1,
                             alias="chat_queue_sqlite_path")


class RedisBootstrapConfig(BaseModel):
    """Redis 队列连接信息；连接凭据不进入 MySQL 运行时配置。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    host: str = Field(
        default="127.0.0.1",
        min_length=1,
        max_length=253,
        alias="chat_redis_host",
    )
    port: int = Field(default=6379, ge=1, le=65535, alias="chat_redis_port")
    database: int = Field(default=0, ge=0, le=15, alias="chat_redis_db")
    password: SecretStr | None = Field(
        default=None,
        alias="chat_redis_password",
        repr=False,
    )
    ssl: bool = Field(default=False, alias="chat_redis_ssl")
    key_prefix: str = Field(
        default="chat",
        min_length=1,
        max_length=64,
        alias="chat_redis_key_prefix",
    )

    @field_validator("password", mode="before")
    @classmethod
    def _normalize_password(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("host")
    @classmethod
    def _validate_host(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9.-]+", value) or value.startswith(('.', '-')):
            raise ValueError("invalid Redis host")
        return value

    @field_validator("key_prefix")
    @classmethod
    def _validate_key_prefix(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9:_-]+", value):
            raise ValueError("invalid Redis key prefix")
        return value


def _env_files(env_file: str | tuple[str, ...] | None) -> tuple[str, ...]:
    if env_file is not None:
        return env_file if isinstance(env_file, tuple) else (env_file,)
    return (".env", ".env.prod")


def _load_environment_values(
    settings_type: type[BaseModel],
    *,
    env_file: str | tuple[str, ...] | None,
) -> dict[str, Any]:
    return BaseSettings._settings_build_values(
        settings_type,
        {},
        env_file=_env_files(env_file),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )


def load_bootstrap_config(
    *,
    env_file: str | tuple[str, ...] | None = None,
) -> BootstrapConfig:
    """只从环境读取不可避免的数据库启动引导配置。"""

    values = _load_environment_values(BootstrapConfig, env_file=env_file)
    return BootstrapConfig.model_validate(values)


def load_redis_bootstrap_config(
    *,
    env_file: str | tuple[str, ...] | None = None,
) -> RedisBootstrapConfig:
    """只从环境读取 Redis 队列启动连接配置。"""

    values = _load_environment_values(RedisBootstrapConfig, env_file=env_file)
    return RedisBootstrapConfig.model_validate(values)


def load_queue_bootstrap_config(
    *, env_file: str | tuple[str, ...] | None = None,
) -> QueueBootstrapConfig:
    """队列选择独立于业务数据库，显式选择前不读取 Redis 连接字段。"""
    return QueueBootstrapConfig.model_validate(
        _load_environment_values(QueueBootstrapConfig, env_file=env_file),
    )


def load_legacy_runtime_config(
    *,
    env_file: str | tuple[str, ...] | None = None,
) -> RuntimeConfig:
    """显式导入旧环境中的业务值，静态字段仍使用禁止插值的启动加载边界。"""

    # 静态集合不能经过 dotenv 插值后再解析，环境源只解析动态字段。
    imported = _load_environment_values(DynamicConfigSource, env_file=env_file)
    values = {name: value for name, value in imported.items() if name in DYNAMIC_SETTING_NAMES}
    static = load_static_config(env_file=env_file)
    return RuntimeConfig.model_validate(values | static_values(static))


def load_static_config(*, env_file: str | tuple[str, ...] | None = None) -> Any:
    """只读取静态字段并保留秘密字面值，文件不执行变量插值，进程环境优先。"""
    values: dict[str, Any] = {}
    sources = [dotenv_values(path, encoding="utf-8", interpolate=False)
               for path in _env_files(env_file)]
    sources.append(dict(os.environ))
    for source in sources:
        normalized = {key.casefold(): value for key, value in source.items()}
        for name in StaticConfig.model_fields:
            if name in normalized:
                values[name] = normalized[name]
    return StaticConfig.model_validate(values)


def database_params(bootstrap: BootstrapConfig) -> DatabaseParams:
    """从 BootstrapConfig 构造连接参数。"""

    return DatabaseParams(
        host=bootstrap.host,
        port=bootstrap.port,
        user=bootstrap.user,
        password=bootstrap.password.get_secret_value(),
        database=bootstrap.database,
        backend=bootstrap.backend,
        sqlite_path=bootstrap.sqlite_path,
    )


def load_startup_settings(bootstrap: BootstrapConfig | None = None) -> VersionedSettings:
    """验证静态配置后先自动升级所选业务库，再加载唯一权威运行配置。"""

    from agent_bot.database_upgrade import upgrade_database

    bootstrap = bootstrap or load_bootstrap_config()
    params = database_params(bootstrap)
    static = load_static_config()
    upgrade_database(params)
    return SettingsStore(params, static_config=static).load()


def nonebot_init_kwargs(config: RuntimeConfig) -> dict[str, Any]:
    """仅返回构造驱动所需且可安全写入 NoneBot DEBUG 日志的配置。"""

    plain_values = runtime_config_to_plain_values(config)
    return {name: plain_values[name] for name in _SAFE_NONEBOT_INIT_FIELDS}


def validate_onebot_driver(driver: Driver, config: RuntimeConfig) -> None:
    """两个启动入口共用能力校验，禁止适配器静默忽略数据库中的正向地址。"""
    if config.onebot_v11_ws_urls and not isinstance(driver, WebSocketClientMixin):
        raise ValueError(
            "正向 OneBot WebSocket 连接需要客户端驱动，请设置 DRIVER=~fastapi+~websockets。"
        )


def scrub_runtime_environment() -> None:
    """快照加载后清理机器人环境字段，避免 NoneBot 二次读取或日志泄露。"""

    runtime_names: set[str] = {"environment"}
    for settings_type in (RuntimeConfig, OneBotConfig):
        for name, field in settings_type.model_fields.items():
            runtime_names.add(name.casefold())
            if isinstance(field.alias, str):
                runtime_names.add(field.alias.casefold())

    for environment_name in tuple(os.environ):
        folded_name = environment_name.casefold()
        if any(
            folded_name == runtime_name or folded_name.startswith(f"{runtime_name}__")
            for runtime_name in runtime_names
        ):
            os.environ.pop(environment_name, None)


def inject_runtime_config(
    global_config: BaseModel,
    config: RuntimeConfig,
    *,
    revision: int | None = None,
) -> None:
    """在 NoneBot 完成初始化日志后，将完整数据库配置注入全局配置。"""

    plain_values = runtime_config_to_plain_values(config)
    for name in RuntimeConfig.model_fields:
        if name in _SAFE_NONEBOT_INIT_FIELDS:
            continue
        runtime_value = getattr(config, name)
        if isinstance(runtime_value, SecretStr):
            runtime_value = plain_values[name]
        setattr(global_config, name, runtime_value)
    if revision is not None:
        if type(revision) is not int or revision < 1:
            raise ValueError("settings revision must be positive")
        global_config.chat_settings_revision = revision
