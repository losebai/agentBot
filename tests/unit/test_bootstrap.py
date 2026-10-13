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


def test_no_backend_uses_sqlite_without_creating_database(tmp_path, monkeypatch):
    """未配置后端默认使用 SQLite，读取配置不能顺带创建数据库文件。"""
    monkeypatch.chdir(tmp_path)
    for name in ("CHAT_DB_BACKEND", "CHAT_DB_USER", "CHAT_DB_PASSWORD", "CHAT_DB_SQLITE_PATH"):
        monkeypatch.delenv(name, raising=False)
    config = load_bootstrap_config(env_file=str(tmp_path / ".env"))
    params = database_params(config)
    assert params.backend == "sqlite"
    assert params.sqlite_path == "data/agent_bot.sqlite3"
    assert not (tmp_path / "data").exists()


def write_legacy_env(path: Path) -> None:
    values = {
        "DRIVER": "~fastapi",
        "HOST": "127.0.0.1",
        "PORT": "8765",
        "LOG_LEVEL": "INFO",
        "COMMAND_START": '["/", "!"]',
        "ONEBOT_V11_ACCESS_TOKEN": ONEBOT_TOKEN,
        "CHAT_SESSION_SECRET": SESSION_SECRET,
        "CHAT_AI_TOKEN": AI_TOKEN,
        "CHAT_ALLOWED_USERS": '["123456"]',
        "CHAT_ALLOWED_GROUPS": '["654321"]',
        "CHAT_NARAKA_COOKIE": NARAKA_COOKIE,
        "CHAT_NARAKA_HEYBOX_ID": "556677",
        "CHAT_NARAKA_DEFAULT_ROLE_ID": "private_role_123",
        "CHAT_AUTO_SPEAK_PROMPT": "private scheduled prompt",
        "CHAT_MEMORY_COMPRESSION_QUEUE_CAPACITY": "17",
        "CHAT_DB_POOL_SIZE": "3",
        "CHAT_DB_TIMEOUT_SECONDS": "13",
        "CHAT_MODEL_CACHE_TTL_SECONDS": "2.5",
        "CHAT_QUEUE_MEDIA_WORKERS": "2",
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
        "CHAT_DB_BACKEND=mysql\n"
        "CHAT_DB_HOST=db.example.test\n"
        "CHAT_DB_PORT=3307\n"
        "CHAT_DB_USER=chat\n"
        "CHAT_DB_PASSWORD=db-secret\n"
        "CHAT_DB_NAME=chat\n"
        f"ONEBOT_V11_ACCESS_TOKEN={ONEBOT_TOKEN}\n",
        encoding="utf-8",
    )

    config = load_bootstrap_config(env_file=env_file)

    assert config == BootstrapConfig(
        backend="mysql",
        host="db.example.test",
        port=3307,
        user="chat",
        password=__import__("pydantic").SecretStr("db-secret"),
        database="chat",
    )
    assert not hasattr(config, "onebot_v11_access_token")


def test_bootstrap_config_requires_database_user(tmp_path: Path) -> None:
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "CHAT_DB_BACKEND=mysql\nCHAT_DB_HOST=db.example.test\nCHAT_DB_PASSWORD=db-secret\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"user"):
        load_bootstrap_config(env_file=env_file)


def test_bootstrap_config_requires_database_password(tmp_path: Path) -> None:
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "CHAT_DB_BACKEND=mysql\nCHAT_DB_HOST=db.example.test\nCHAT_DB_USER=chat\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"password"):
        load_bootstrap_config(env_file=env_file)


def test_bootstrap_defaults_port_and_database(tmp_path: Path) -> None:
    """显式使用 MySQL 时仍保留其默认端口和库名。"""
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "CHAT_DB_BACKEND=mysql\nCHAT_DB_USER=chat\nCHAT_DB_PASSWORD=db-secret\n",
        encoding="utf-8",
    )

    config = load_bootstrap_config(env_file=env_file)

    assert config.host == "127.0.0.1"
    assert config.port == 3306
    assert config.database == "chat"


def test_redis_bootstrap_reads_connection_without_exposing_password(tmp_path: Path) -> None:
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "CHAT_REDIS_HOST=redis.internal.test\n"
        "CHAT_REDIS_PORT=6380\n"
        "CHAT_REDIS_DB=5\n"
        "CHAT_REDIS_PASSWORD=redis-private-marker\n"
        "CHAT_REDIS_SSL=true\n"
        "CHAT_REDIS_KEY_PREFIX=prod:chat\n",
        encoding="utf-8",
    )

    config = load_redis_bootstrap_config(env_file=env_file)

    assert config.host == "redis.internal.test"
    assert config.port == 6380
    assert config.database == 5
    assert config.ssl is True
    assert config.key_prefix == "prod:chat"
    assert config.password is not None
    assert config.password.get_secret_value() == "redis-private-marker"
    assert "redis-private-marker" not in repr(config)


def test_redis_bootstrap_has_local_non_secret_defaults(tmp_path: Path) -> None:
    config = load_redis_bootstrap_config(env_file=tmp_path / "missing.env")

    assert config == RedisBootstrapConfig()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("CHAT_REDIS_HOST", "bad host"),
        ("CHAT_REDIS_PORT", "0"),
        ("CHAT_REDIS_DB", "16"),
        ("CHAT_REDIS_KEY_PREFIX", "unsafe key prefix"),
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
    """传递 MySQL 参数时仅在连接边界解包密码。"""
    env_file = tmp_path / "bootstrap.env"
    env_file.write_text(
        "CHAT_DB_BACKEND=mysql\nCHAT_DB_USER=chat\nCHAT_DB_PASSWORD=db-secret\n",
        encoding="utf-8",
    )
    config = load_bootstrap_config(env_file=env_file)

    params = database_params(config)

    assert params.host == "127.0.0.1"
    assert params.port == 3306
    assert params.user == "chat"
    assert params.password == "db-secret"
    assert params.database == "chat"


def test_legacy_environment_is_validated_as_complete_runtime_snapshot(tmp_path: Path) -> None:
    env_file = tmp_path / "legacy.env"
    write_legacy_env(env_file)

    config = load_legacy_runtime_config(env_file=env_file)

    assert config.port == 8765
    assert config.command_start == {"/", "!"}
    assert config.onebot_v11_access_token.get_secret_value() == ONEBOT_TOKEN
    assert config.chat_session_secret.get_secret_value() == SESSION_SECRET
    assert config.chat_allowed_users == {"123456"}
    assert config.chat_allowed_groups == {"654321"}
    assert config.chat_naraka_cookie is not None
    assert config.chat_naraka_cookie.get_secret_value() == NARAKA_COOKIE
    assert config.chat_memory_compression_queue_capacity == 17
    assert config.chat_db_pool_size == 3
    assert config.chat_db_timeout_seconds == 13
    assert config.chat_queue_media_workers == 2


def test_legacy_environment_validation_error_never_contains_secret(tmp_path: Path) -> None:
    env_file = tmp_path / "legacy.env"
    leaked = "too-short-and-private"
    env_file.write_text(
        f"ONEBOT_V11_ACCESS_TOKEN={leaked}\nCHAT_SESSION_SECRET={SESSION_SECRET}\n",
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


def test_example_imports_after_replacing_required_credentials(tmp_path: Path) -> None:
    """真实示例的受约束 JSON 列表、空白可选文本和秒数必须可直接导入。"""
    from datetime import timedelta

    example = (Path(__file__).parents[2] / ".env.example").read_text("utf-8")
    example = example.replace("REPLACE_ME_TOO", SESSION_SECRET).replace("REPLACE_ME", ONEBOT_TOKEN)
    env_file = tmp_path / "example.env"
    env_file.write_text(example, encoding="utf-8")

    config = load_legacy_runtime_config(env_file=str(env_file))

    assert config.chat_mcp_services == []
    assert config.chat_mcp_allowed_hosts == ["127.0.0.1:*", "localhost:*", "chat:*"]
    assert config.chat_tts_reference_audio_path is None
    assert config.chat_tts_reference_text is None
    assert config.session_expire_timeout == timedelta(seconds=120)


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
    assert restored.chat_session_secret.get_secret_value() == SESSION_SECRET
    assert restored.chat_allowed_users == {"123456"}
    assert restored.chat_allowed_groups == {"654321"}
    assert global_config.chat_settings_revision == 7
    assert restored.chat_memory_compression_queue_capacity == 17
    assert restored.chat_db_pool_size == 3
    assert restored.chat_db_timeout_seconds == 13
    assert restored.chat_queue_media_workers == 2


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
    monkeypatch.setenv("CHAT_TTS_TOKEN", "environment-tts-token")
    monkeypatch.setenv("ENVIRONMENT", "environment-must-not-select-config")
    monkeypatch.setenv("CHAT_DB_HOST", "bootstrap-must-remain")

    scrub_runtime_environment()

    assert not any(
        name.casefold()
        in {
            "nickname",
            "onebot_v11_ws_urls",
            "onebot_api_roots__environment_bot",
            "onebot_access_token",
            "chat_tts_token",
            "environment",
        }
        for name in os.environ
    )
    assert os.environ["CHAT_DB_HOST"] == "bootstrap-must-remain"


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
