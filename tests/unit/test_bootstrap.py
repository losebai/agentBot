"""验证引导配置隔离，以及新增媒体字段完整进入持久化运行配置。"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from nonebot.adapters.onebot.v11.config import Config as OneBotConfig
from nonebot.compat import model_dump
from nonebot.config import Config as NoneBotConfig

from agent_bot.bootstrap import (
    BootstrapConfig,
    RedisBootstrapConfig,
    database_params,
    inject_runtime_config,
    load_bootstrap_config,
    load_legacy_runtime_config,
    load_redis_bootstrap_config,
    nonebot_init_kwargs,
    scrub_runtime_environment,
)
from agent_bot.config import RuntimeConfig

ONEBOT_TOKEN = "legacy-onebot-token-that-is-longer-than-thirty-two-characters"
SESSION_SECRET = "legacy-session-secret-that-is-longer-than-thirty-two-characters"
AI_TOKEN = "private-ai-token-for-bootstrap-test"
NARAKA_COOKIE = (
    "pkey=private-pkey; x_heybox_id=556677; x_pkey=private-x-pkey; x_xhh_tokenid=private-token-id"
)


def write_legacy_env(path: Path) -> None:
    values = {
        "DRIVER": "~fastapi",
        "HOST": "127.0.0.1",
        "PORT": "8765",
        "LOG_LEVEL": "INFO",
        "COMMAND_START": '["/", "!"]',
        "ONEBOT_V11_ACCESS_TOKEN": ONEBOT_TOKEN,
        "DISPATCH_CHAT_SESSION_SECRET": SESSION_SECRET,
        "DISPATCH_CHAT_AI_TOKEN": AI_TOKEN,
        "DISPATCH_CHAT_ALLOWED_USERS": '["123456"]',
        "DISPATCH_CHAT_ALLOWED_GROUPS": '["654321"]',
        "DISPATCH_CHAT_NARAKA_COOKIE": NARAKA_COOKIE,
        "DISPATCH_CHAT_NARAKA_HEYBOX_ID": "556677",
        "DISPATCH_CHAT_NARAKA_DEFAULT_ROLE_ID": "private_role_123",
        "DISPATCH_CHAT_AUTO_SPEAK_PROMPT": "private scheduled prompt",
        "DISPATCH_CHAT_MEMORY_COMPRESSION_QUEUE_CAPACITY": "17",
        "DISPATCH_CHAT_DB_POOL_SIZE": "3",
        "DISPATCH_CHAT_DB_TIMEOUT_SECONDS": "13",
        "DISPATCH_CHAT_MODEL_CACHE_TTL_SECONDS": "2.5",
        "DISPATCH_CHAT_QUEUE_MEDIA_WORKERS": "2",
    }
    path.write_text(
        "\n".join(
            f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in values.items()
        ),
        encoding="utf-8",
    )


def test_bootstrap_config_reads_only_database_connection(tmp_path: Path) -> None:
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "DISPATCH_CHAT_DB_HOST=db.example.test\n"
        "DISPATCH_CHAT_DB_PORT=3307\n"
        "DISPATCH_CHAT_DB_USER=dispatch_chat\n"
        "DISPATCH_CHAT_DB_PASSWORD=db-secret\n"
        "DISPATCH_CHAT_DB_NAME=dispatch_chat\n"
        f"ONEBOT_V11_ACCESS_TOKEN={ONEBOT_TOKEN}\n",
        encoding="utf-8",
    )

    config = load_bootstrap_config(env_file=env_file)

    assert config == BootstrapConfig(
        host="db.example.test",
        port=3307,
        user="dispatch_chat",
        password=__import__("pydantic").SecretStr("db-secret"),
        database="dispatch_chat",
    )
    assert not hasattr(config, "onebot_v11_access_token")


def test_bootstrap_config_requires_database_user(tmp_path: Path) -> None:
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "DISPATCH_CHAT_DB_HOST=db.example.test\nDISPATCH_CHAT_DB_PASSWORD=db-secret\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"user"):
        load_bootstrap_config(env_file=env_file)


def test_bootstrap_config_requires_database_password(tmp_path: Path) -> None:
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "DISPATCH_CHAT_DB_HOST=db.example.test\nDISPATCH_CHAT_DB_USER=dispatch_chat\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"password"):
        load_bootstrap_config(env_file=env_file)


def test_bootstrap_defaults_port_and_database(tmp_path: Path) -> None:
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "DISPATCH_CHAT_DB_USER=dispatch_chat\nDISPATCH_CHAT_DB_PASSWORD=db-secret\n",
        encoding="utf-8",
    )

    config = load_bootstrap_config(env_file=env_file)

    assert config.host == "127.0.0.1"
    assert config.port == 3306
    assert config.database == "dispatch_chat"


def test_redis_bootstrap_reads_connection_without_exposing_password(tmp_path: Path) -> None:
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "DISPATCH_CHAT_REDIS_HOST=redis.internal.test\n"
        "DISPATCH_CHAT_REDIS_PORT=6380\n"
        "DISPATCH_CHAT_REDIS_DB=5\n"
        "DISPATCH_CHAT_REDIS_PASSWORD=redis-private-marker\n"
        "DISPATCH_CHAT_REDIS_SSL=true\n"
        "DISPATCH_CHAT_REDIS_KEY_PREFIX=dispatch:prod:chat\n",
        encoding="utf-8",
    )

    config = load_redis_bootstrap_config(env_file=env_file)

    assert config.host == "redis.internal.test"
    assert config.port == 6380
    assert config.database == 5
    assert config.ssl is True
    assert config.key_prefix == "dispatch:prod:chat"
    assert config.password is not None
    assert config.password.get_secret_value() == "redis-private-marker"
    assert "redis-private-marker" not in repr(config)


def test_redis_bootstrap_has_local_non_secret_defaults(tmp_path: Path) -> None:
    config = load_redis_bootstrap_config(env_file=tmp_path / "missing.env")

    assert config == RedisBootstrapConfig()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DISPATCH_CHAT_REDIS_HOST", "bad host"),
        ("DISPATCH_CHAT_REDIS_PORT", "0"),
        ("DISPATCH_CHAT_REDIS_DB", "16"),
        ("DISPATCH_CHAT_REDIS_KEY_PREFIX", "unsafe key prefix"),
    ],
)
def test_redis_bootstrap_rejects_unsafe_values(
    tmp_path: Path,
    name: str,
    value: str,
) -> None:
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(f"{name}={value}\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_redis_bootstrap_config(env_file=env_file)


def test_database_params_unpacks_secret(tmp_path: Path) -> None:
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "DISPATCH_CHAT_DB_USER=dispatch_chat\nDISPATCH_CHAT_DB_PASSWORD=db-secret\n",
        encoding="utf-8",
    )
    config = load_bootstrap_config(env_file=env_file)

    params = database_params(config)

    assert params.host == "127.0.0.1"
    assert params.port == 3306
    assert params.user == "dispatch_chat"
    assert params.password == "db-secret"
    assert params.database == "dispatch_chat"


def test_legacy_environment_is_validated_as_complete_runtime_snapshot(tmp_path: Path) -> None:
    env_file = tmp_path / "legacy.env"
    write_legacy_env(env_file)

    config = load_legacy_runtime_config(env_file=env_file)

    assert config.port == 8765
    assert config.command_start == {"/", "!"}
    assert config.onebot_v11_access_token.get_secret_value() == ONEBOT_TOKEN
    assert config.dispatch_chat_session_secret.get_secret_value() == SESSION_SECRET
    assert config.dispatch_chat_allowed_users == {"123456"}
    assert config.dispatch_chat_allowed_groups == {"654321"}
    assert config.dispatch_chat_naraka_cookie is not None
    assert config.dispatch_chat_naraka_cookie.get_secret_value() == NARAKA_COOKIE
    assert config.dispatch_chat_memory_compression_queue_capacity == 17
    assert config.dispatch_chat_db_pool_size == 3
    assert config.dispatch_chat_db_timeout_seconds == 13
    assert config.dispatch_chat_model_cache_ttl_seconds == 2.5
    assert config.dispatch_chat_queue_media_workers == 2


def test_legacy_environment_validation_error_never_contains_secret(tmp_path: Path) -> None:
    env_file = tmp_path / "legacy.env"
    leaked = "too-short-and-private"
    env_file.write_text(
        f"ONEBOT_V11_ACCESS_TOKEN={leaked}\nDISPATCH_CHAT_SESSION_SECRET={SESSION_SECRET}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError) as exc_info:
        load_legacy_runtime_config(env_file=env_file)

    assert leaked not in str(exc_info.value)


def test_example_secrets_must_be_replaced_before_import() -> None:
    example_path = Path(__file__).parents[2] / ".env.example"

    with pytest.raises(ValueError) as exc_info:
        load_legacy_runtime_config(env_file=example_path)

    rendered = str(exc_info.value)
    assert "REPLACE_ME" not in rendered


def test_nonebot_init_kwargs_only_contain_non_sensitive_startup_values(tmp_path: Path) -> None:
    env_file = tmp_path / "legacy.env"
    write_legacy_env(env_file)
    config = load_legacy_runtime_config(env_file=env_file)

    kwargs = nonebot_init_kwargs(config)

    assert set(kwargs) == {
        "driver",
        "host",
        "port",
        "log_level",
        "api_timeout",
        "command_start",
        "command_sep",
        "session_expire_timeout",
    }
    assert kwargs["port"] == 8765
    assert set(kwargs["command_start"]) == {"/", "!"}

    rendered = repr(kwargs)
    for private_value in (
        ONEBOT_TOKEN,
        SESSION_SECRET,
        AI_TOKEN,
        NARAKA_COOKIE,
        "123456",
        "654321",
        "private_role_123",
        "private scheduled prompt",
    ):
        assert private_value not in rendered


def test_runtime_config_is_injected_after_nonebot_initialization(tmp_path: Path) -> None:
    env_file = tmp_path / "legacy.env"
    write_legacy_env(env_file)
    runtime_config = load_legacy_runtime_config(env_file=env_file)
    kwargs = nonebot_init_kwargs(runtime_config)

    global_config = NoneBotConfig(_env_file=None, **kwargs)
    inject_runtime_config(global_config, runtime_config, revision=7)
    dumped = model_dump(global_config)

    restored = RuntimeConfig.model_validate(dumped)
    adapter_config = OneBotConfig.model_validate(dumped)
    assert adapter_config.onebot_access_token == ONEBOT_TOKEN
    assert restored.dispatch_chat_session_secret.get_secret_value() == SESSION_SECRET
    assert restored.dispatch_chat_allowed_users == {"123456"}
    assert restored.dispatch_chat_allowed_groups == {"654321"}
    assert global_config.dispatch_chat_settings_revision == 7
    assert restored.dispatch_chat_memory_compression_queue_capacity == 17
    assert restored.dispatch_chat_db_pool_size == 3
    assert restored.dispatch_chat_db_timeout_seconds == 13
    assert restored.dispatch_chat_model_cache_ttl_seconds == 2.5
    assert restored.dispatch_chat_queue_media_workers == 2


def test_runtime_environment_is_scrubbed_case_insensitively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NICKNAME", '["environment-nickname"]')
    monkeypatch.setenv("onebot_v11_ws_urls", '["wss://environment.invalid/ws"]')
    monkeypatch.setenv(
        "ONEBOT_API_ROOTS__environment_bot",
        "https://environment.invalid/api",
    )
    monkeypatch.setenv("onebot_access_token", "environment-onebot-token")
    monkeypatch.setenv("DISPATCH_CHAT_AI_TOKEN", "environment-ai-token")
    monkeypatch.setenv("ENVIRONMENT", "environment-must-not-select-config")
    monkeypatch.setenv("DISPATCH_CHAT_DB_HOST", "bootstrap-must-remain")

    scrub_runtime_environment()

    assert not any(
        name.casefold()
        in {
            "nickname",
            "onebot_v11_ws_urls",
            "onebot_api_roots__environment_bot",
            "onebot_access_token",
            "dispatch_chat_ai_token",
            "environment",
        }
        for name in os.environ
    )
    assert os.environ["DISPATCH_CHAT_DB_HOST"] == "bootstrap-must-remain"


def test_runtime_config_storage_contract_covers_all_plugin_and_nonebot_fields() -> None:
    """字段集合不能漏掉插件、适配器或新增的媒体路由开关。"""
    expected_nonebot_fields = set(NoneBotConfig.model_fields)
    expected_onebot_fields = {
        "onebot_v11_secret",
        "onebot_v11_ws_urls",
        "onebot_v11_api_roots",
    }
    plugin_fields = set(RuntimeConfig.__mro__[1].model_fields)

    assert set(RuntimeConfig.model_fields) == (
        plugin_fields | expected_nonebot_fields | expected_onebot_fields
    )
    assert len(RuntimeConfig.model_fields) == 140
    assert "dispatch_chat_media_backend" in plugin_fields
