"""MySQL 配置库的启动引导与 NoneBot 注入。"""

from __future__ import annotations

import os
import re
from typing import Any

from nonebot.adapters.onebot.v11.config import Config as OneBotConfig
from nonebot.config import BaseSettings
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from agent_bot.config import RuntimeConfig
from agent_bot.settings import (
    DatabaseParams,
    SettingsStore,
    VersionedSettings,
    runtime_config_to_plain_values,
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
    """MySQL 连接信息；这些不能存入被数据库管理的配置本身。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    host: str = Field(default="127.0.0.1", alias="dispatch_chat_db_host")
    port: int = Field(default=3306, ge=1, le=65535, alias="dispatch_chat_db_port")
    user: str = Field(alias="dispatch_chat_db_user")
    password: SecretStr = Field(alias="dispatch_chat_db_password")
    database: str = Field(default="dispatch_chat", alias="dispatch_chat_db_name")


class RedisBootstrapConfig(BaseModel):
    """Redis 队列连接信息；连接凭据不进入 MySQL 运行时配置。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    host: str = Field(
        default="127.0.0.1",
        min_length=1,
        max_length=253,
        alias="dispatch_chat_redis_host",
    )
    port: int = Field(default=6379, ge=1, le=65535, alias="dispatch_chat_redis_port")
    database: int = Field(default=0, ge=0, le=15, alias="dispatch_chat_redis_db")
    password: SecretStr | None = Field(
        default=None,
        alias="dispatch_chat_redis_password",
        repr=False,
    )
    ssl: bool = Field(default=False, alias="dispatch_chat_redis_ssl")
    key_prefix: str = Field(
        default="dispatch:chat",
        min_length=1,
        max_length=64,
        alias="dispatch_chat_redis_key_prefix",
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


def load_legacy_runtime_config(
    *,
    env_file: str | tuple[str, ...] | None = None,
) -> RuntimeConfig:
    """读取旧 dotenv，用于显式、一次性的数据库导入。"""

    values = _load_environment_values(RuntimeConfig, env_file=env_file)
    return RuntimeConfig.model_validate(values)


def database_params(bootstrap: BootstrapConfig) -> DatabaseParams:
    """从 BootstrapConfig 构造连接参数。"""

    return DatabaseParams(
        host=bootstrap.host,
        port=bootstrap.port,
        user=bootstrap.user,
        password=bootstrap.password.get_secret_value(),
        database=bootstrap.database,
    )


def load_startup_settings(bootstrap: BootstrapConfig | None = None) -> VersionedSettings:
    """从数据库加载唯一权威配置；失败时不会回退旧环境。"""

    bootstrap = bootstrap or load_bootstrap_config()
    return SettingsStore(database_params(bootstrap)).load()


def nonebot_init_kwargs(config: RuntimeConfig) -> dict[str, Any]:
    """仅返回构造驱动所需且可安全写入 NoneBot DEBUG 日志的配置。"""

    plain_values = runtime_config_to_plain_values(config)
    return {name: plain_values[name] for name in _SAFE_NONEBOT_INIT_FIELDS}


def scrub_runtime_environment() -> None:
    """删除旧运行配置环境变量，防止插件绕过数据库重新读取它们。"""

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
        global_config.dispatch_chat_settings_revision = revision
