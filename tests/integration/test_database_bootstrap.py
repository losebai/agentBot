from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent_bot.config import RuntimeConfig
from agent_bot.settings import DatabaseParams, SettingsStore


def test_auto_speak_trigger_mode_migration_is_idempotent_and_revisioned() -> None:
    """Pin the offline migration contract without requiring a real database."""

    project_root = Path(__file__).resolve().parents[2]
    sql = (project_root / "sql/legacy" / "2026-09-29-auto-speak-trigger-mode.sql").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(sql.split()).casefold()

    assert "get_lock('dispatch_chat_setting', 5)" in normalized
    assert "'dispatch_chat_auto_speak_trigger_mode'" in normalized
    assert "'\"interval\"'" in normalized
    assert "on duplicate key update" in normalized
    assert "description" in normalized
    assert "dispatch_chat_setting_revision" in normalized
    assert "revision` = `revision` + 1" in normalized
    assert "release_lock('dispatch_chat_setting')" in normalized
    for forbidden in ("api_key", "bearer ", "token=", "password="):
        assert forbidden not in normalized


def test_auto_speak_laya_prompt_migration_is_idempotent_and_revisioned() -> None:
    """Pin the Laya decision prompt migration without requiring a real database."""

    project_root = Path(__file__).resolve().parents[2]
    sql = (project_root / "sql/legacy" / "2026-09-30-auto-speak-laya-prompt.sql").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(sql.split()).casefold()
    duplicate_clause = normalized.split("on duplicate key update", maxsplit=1)[1]

    assert "get_lock('dispatch_chat_setting', 5)" in normalized
    assert "'dispatch_chat_auto_speak_laya_prompt'" in normalized
    assert "speak" in normalized
    assert "silent" in normalized
    assert "`description` = values(`description`)" in duplicate_clause
    assert "`config_value` = values(`config_value`)" not in duplicate_clause
    assert "dispatch_chat_setting_revision" in normalized
    assert "revision` = `revision` + 1" in normalized
    assert "release_lock('dispatch_chat_setting')" in normalized
    for forbidden in ("api_key", "bearer ", "token=", "password="):
        assert forbidden not in normalized

# 本集成测试需要真实可达的 MySQL 实例，且 chat_setting 表已通过
# sql/agent_bot_init.sql 初始化。通过以下环境变量提供连接信息：
#   CHAT_INTEGRATION_DB_HOST (默认 127.0.0.1)
#   CHAT_INTEGRATION_DB_PORT (默认 3306)
#   CHAT_INTEGRATION_DB_USER
#   CHAT_INTEGRATION_DB_PASSWORD
#   CHAT_INTEGRATION_DB_NAME (默认 chat_test)
# 若未配置用户名/密码，测试会被跳过。


def _integration_params() -> DatabaseParams | None:
    user = os.environ.get("CHAT_INTEGRATION_DB_USER")
    password = os.environ.get("CHAT_INTEGRATION_DB_PASSWORD")
    if not user or not password:
        return None
    return DatabaseParams(
        host=os.environ.get("CHAT_INTEGRATION_DB_HOST", "127.0.0.1"),
        port=int(os.environ.get("CHAT_INTEGRATION_DB_PORT", "3306")),
        user=user,
        password=password,
        database=os.environ.get("CHAT_INTEGRATION_DB_NAME", "chat_test"),
    )


@pytest.mark.integration
def test_database_onebot_token_reaches_adapter_without_log_disclosure() -> None:
    params = _integration_params()
    if params is None:
        pytest.skip("set CHAT_INTEGRATION_DB_USER/PASSWORD to run this integration test")

    # 每次运行前清空旧配置，保证 initialize 不会因已存在快照而失败。
    import pymysql

    connection = pymysql.connect(
        host=params.host,
        port=params.port,
        user=params.user,
        password=params.password,
        database=params.database,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM chat_setting")
            cursor.execute("DELETE FROM chat_setting_revision")
        connection.commit()
    finally:
        connection.close()

    token = "database-adapter-token-that-is-longer-than-thirty-two-characters"
    onebot_secret = "database-onebot-signing-secret"
    superuser = "940000000000007"
    nickname = "database-private-nickname"
    ws_url = "wss://database-onebot.example.invalid/private-ws"
    api_url = "https://database-onebot.example.invalid/private-api"
    tts_token = "database-private-tts-token"
    allowed_user = "940000000000008"
    SettingsStore(params).initialize(
        RuntimeConfig(
            onebot_v11_access_token=token,
            chat_session_secret=(
                "database-adapter-session-secret-longer-than-thirty-two-characters"
            ),
            log_level="DEBUG",
            superusers={superuser},
            nickname={nickname},
            onebot_v11_secret=onebot_secret,
            onebot_v11_ws_urls={ws_url},
            onebot_v11_api_roots={"database-bot": api_url},
            chat_tts_token=tts_token,
            chat_allowed_users={allowed_user},
        )
    )
    environment = os.environ.copy()
    for field_name in RuntimeConfig.model_fields:
        environment.pop(field_name.upper(), None)
    environment["CHAT_DB_HOST"] = params.host
    environment["CHAT_DB_PORT"] = str(params.port)
    environment["CHAT_DB_USER"] = params.user
    environment["CHAT_DB_PASSWORD"] = params.password
    environment["CHAT_DB_NAME"] = params.database
    environment["NICKNAME"] = '["environment-must-not-win"]'
    environment["ONEBOT_ACCESS_TOKEN"] = "environment-must-not-win"
    environment["ONEBOT_API_ROOTS__environment-bot"] = "https://environment.invalid/api"
    environment["CHAT_TTS_TOKEN"] = "environment-must-not-win"
    environment["CHAT_ALLOWED_USERS"] = '["999999999"]'
    script = """
import os
import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from agent_bot.bootstrap import (
    inject_runtime_config,
    load_startup_settings,
    nonebot_init_kwargs,
    scrub_runtime_environment,
)

stored = load_startup_settings()
scrub_runtime_environment()
nonebot.init(_env_file=(os.devnull,), **nonebot_init_kwargs(stored.config))
driver = nonebot.get_driver()
inject_runtime_config(driver.config, stored.config, revision=stored.revision)
driver.register_adapter(Adapter)
adapter = nonebot.get_adapter(Adapter)
assert adapter.onebot_config.onebot_access_token == (
    stored.config.onebot_v11_access_token.get_secret_value()
)
assert driver.config.nickname == stored.config.nickname
assert adapter.onebot_config.onebot_secret == (
    stored.config.onebot_v11_secret.get_secret_value()
)
assert adapter.onebot_config.onebot_ws_urls == stored.config.onebot_v11_ws_urls
assert adapter.onebot_config.onebot_api_roots == stored.config.onebot_v11_api_roots
plugin = nonebot.load_plugin("agent_bot.plugin")
assert plugin is not None
from agent_bot import plugin as plugin_module
assert plugin_module.plugin_config.chat_tts_token.get_secret_value() == (
    stored.config.chat_tts_token.get_secret_value()
)
assert plugin_module.plugin_config.chat_allowed_users == (
    stored.config.chat_allowed_users
)
print("adapter-ready")
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "adapter-ready" in output
    for private_value in (
        token,
        onebot_secret,
        superuser,
        nickname,
        ws_url,
        api_url,
        tts_token,
        allowed_user,
        "environment-must-not-win",
    ):
        assert private_value not in output
