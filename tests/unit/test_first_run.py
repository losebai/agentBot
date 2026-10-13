"""首次启动使用真实临时 SQLite，验证本地默认值与已有配置保护。"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from dotenv import dotenv_values

from agent_bot.bootstrap import load_bootstrap_config, load_static_config
from agent_bot.settings import DatabaseParams, SettingsStore


@pytest.fixture(autouse=True)
def clean_startup_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """屏蔽开发机启动配置，保证测试只接触临时目录。"""
    from agent_bot.static_config import STATIC_SETTING_NAMES

    for name in tuple(os.environ):
        if name.lower() in STATIC_SETTING_NAMES or name.upper().startswith(
            ("CHAT_DB_", "CHAT_REDIS_", "CHAT_QUEUE_", "AGENT_BOT_", "ONEBOT_")
        ):
            monkeypatch.delenv(name)


def test_first_run_creates_valid_local_settings_and_preserves_changes(tmp_path: Path) -> None:
    """首次生成可读默认快照，再次启动保留密钥与用户修改后的数据库。"""
    from agent_bot.first_run import prepare_first_run

    (tmp_path / ".env.example").write_text("CHAT_MODEL=should-not-import\n", encoding="utf-8")
    prepare_first_run(tmp_path)

    env_file = tmp_path / ".env"
    initial_env = env_file.read_bytes()
    values = dotenv_values(env_file)
    bootstrap = load_bootstrap_config(env_file=str(env_file))
    assert bootstrap.backend == "sqlite"
    assert values["CHAT_QUEUE_BACKEND"] == "sqlite"
    assert values["AGENT_BOT_ADMIN_TOKEN"] == ""
    assert len(values["CHAT_SESSION_SECRET"]) >= 64
    assert len(values["ONEBOT_V11_ACCESS_TOKEN"]) >= 64
    assert values["CHAT_SESSION_SECRET"] != values["ONEBOT_V11_ACCESS_TOKEN"]
    store = SettingsStore(
        DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / bootstrap.sqlite_path)),
        static_config=load_static_config(env_file=str(env_file)),
    )
    loaded = store.load()
    assert loaded.revision == 1
    assert loaded.config.driver == "~fastapi+~websockets"
    assert str(loaded.config.host) == "127.0.0.1"
    assert loaded.config.port == 8080
    assert loaded.config.chat_model != "should-not-import"
    changed = loaded.config.model_copy(update={"chat_system_prompt": "保留用户设置"})
    store.save(changed, expected_revision=1)
    prepare_first_run(tmp_path)
    assert env_file.read_bytes() == initial_env
    assert store.load().config.chat_system_prompt == "保留用户设置"
    assert store.load_revision() == 2


@pytest.mark.parametrize("filename", [".env", ".env.prod"])
def test_existing_config_is_user_owned_even_when_empty(tmp_path: Path, filename: str) -> None:
    """用户已建立的配置文件不能被自动补写或触发数据库初始化。"""
    from agent_bot.first_run import prepare_first_run

    config_file = tmp_path / filename
    config_file.write_text("", encoding="utf-8")
    prepare_first_run(tmp_path)
    assert config_file.read_bytes() == b""
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("filename", ["agent_bot.sqlite3", "queue.sqlite3"])
def test_missing_config_with_existing_database_requires_recovery(
    tmp_path: Path, filename: str,
) -> None:
    """配置丢失时必须阻止生成新密钥，同时保留已有数据库的所有字节。"""
    from agent_bot.first_run import FirstRunError, prepare_first_run

    database = tmp_path / "data" / filename
    database.parent.mkdir()
    database.write_bytes(b"existing database must remain untouched")
    with pytest.raises(FirstRunError, match=r"恢复.*配置"):
        prepare_first_run(tmp_path)
    assert database.read_bytes() == b"existing database must remain untouched"
    assert not (tmp_path / ".env").exists()


@pytest.mark.parametrize(
    "name,value",
    [("CHAT_DB_BACKEND", "mysql"), ("CHAT_DB_SQLITE_PATH", "custom.db"),
     ("CHAT_REDIS_HOST", "redis.internal"), ("CHAT_QUEUE_BACKEND", "redis"),
     ("CHAT_SESSION_SECRET", "existing-secret"), ("AGENT_BOT_ADMIN_TOKEN", "")],
)
def test_external_environment_disables_automatic_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str,
) -> None:
    """即使值为空，显式部署配置仍由用户负责，不能自动切换为本地部署。"""
    from agent_bot.first_run import prepare_first_run

    monkeypatch.setenv(name, value)
    prepare_first_run(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_listener_environment_can_override_first_run_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """端口探测和日志设置不会阻止首启，环境仍具有启动优先权。"""
    from agent_bot.first_run import prepare_first_run

    monkeypatch.setenv("PORT", "18765")
    prepare_first_run(tmp_path)
    assert load_static_config(env_file=str(tmp_path / ".env")).port == 18765


def test_failed_initialization_does_not_publish_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """底层初始化失败后不发布残缺配置，并提供无密钥的明确错误。"""
    from agent_bot import database
    from agent_bot.first_run import FirstRunError, prepare_first_run

    def fail_initialization(params: DatabaseParams) -> None:
        """模拟磁盘写入途中失败，检查暂存产物能否安全清理。"""
        with closing(sqlite3.connect(params.sqlite_path)) as connection:
            connection.execute("CREATE TABLE incomplete (id INTEGER)")
        raise RuntimeError("sensitive database failure")

    monkeypatch.setattr(database, "initialize_database", fail_initialization)
    with pytest.raises(FirstRunError, match="首次初始化失败") as error:
        prepare_first_run(tmp_path)
    assert "sensitive" not in str(error.value)
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "data" / "agent_bot.sqlite3").exists()
    assert not list(tmp_path.glob(".agent-bot-first-run-*"))


def test_config_publication_conflict_preserves_user_file_and_generated_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发产生配置文件时不得覆盖，已生成密钥留在暂存目录以供恢复。"""
    from agent_bot import first_run

    original_link = os.link

    def publish_with_conflict(source: Path, target: Path) -> None:
        """模拟其他进程恰好在发布配置前创建了自己的文件。"""
        if target.name == ".env":
            target.write_text("user owned configuration", encoding="utf-8")
        original_link(source, target)

    monkeypatch.setattr(first_run.os, "link", publish_with_conflict)
    with pytest.raises(first_run.FirstRunError, match="恢复"):
        first_run.prepare_first_run(tmp_path)
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "user owned configuration"
    assert (tmp_path / "data" / "agent_bot.sqlite3").exists()
    staging = next(tmp_path.glob(".agent-bot-first-run-*"))
    original_secrets = (staging / ".env").read_bytes()
    (tmp_path / ".env").unlink()
    with pytest.raises(first_run.FirstRunError, match=r"恢复.*配置"):
        first_run.prepare_first_run(tmp_path)
    assert (staging / ".env").read_bytes() == original_secrets


def test_interrupted_staging_requires_explicit_recovery(tmp_path: Path) -> None:
    """崩溃遗留的密钥暂存目录不会被下一次首启清理或换成新密钥。"""
    from agent_bot.first_run import FirstRunError, prepare_first_run

    staging = tmp_path / ".agent-bot-first-run-interrupted"
    staging.mkdir()
    (staging / ".env").write_text("original secrets", encoding="utf-8")
    with pytest.raises(FirstRunError, match="恢复原配置"):
        prepare_first_run(tmp_path)
    assert (staging / ".env").read_text(encoding="utf-8") == "original secrets"
    assert not (tmp_path / ".env").exists()
