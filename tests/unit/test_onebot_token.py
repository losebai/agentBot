"""OneBot Token 的静态权威来源、管理只读边界和独立首次密钥验收。"""

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_configuration_layers import config

from agent_bot.admin.settings import SettingsService
from agent_bot.bootstrap import load_startup_settings, load_static_config
from agent_bot.config_cli import run
from agent_bot.database import initialize_database
from agent_bot.settings import DatabaseParams, SettingsStore

NEW_TOKEN = "new-static-onebot-token-with-at-least-32-characters"


def _store(tmp_path: Path) -> SettingsStore:
    """所有验收使用独立 SQLite，不连接现有服务或修改真实配置。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.sqlite3"))
    initialize_database(params)
    store = SettingsStore(params, static_config=config())
    store.initialize(config())
    return store


def _legacy_token(store: SettingsStore) -> None:
    """模拟历史库残留凭据，读取启动配置不能迁移、覆盖或使用此旧值。"""
    with closing(sqlite3.connect(store.params.sqlite_path)) as connection:
        connection.execute(
            "INSERT INTO chat_setting (config_key,config_value) VALUES(?,?) "
            "ON CONFLICT(config_key) DO UPDATE SET config_value=excluded.config_value",
            ("onebot_v11_access_token", json.dumps("legacy-database-token-at-least-32-characters")),
        )
        connection.commit()


def test_token_is_static_readonly_redacted_and_not_stored(tmp_path: Path) -> None:
    """协议与会话密钥只展示已设置状态，运行数据库不保存协议 Token。"""
    store = _store(tmp_path)
    snapshot = SettingsService(store, SimpleNamespace(config=config(), revision=1)).read()
    for name in ("onebot_v11_access_token", "chat_session_secret"):
        field = next(row for row in snapshot.fields if row.name == name)
        assert field.scope == "static" and field.read_only and field.restart_required
        assert field.value is None and field.secret_is_set
    assert config().onebot_v11_access_token.get_secret_value() not in snapshot.model_dump_json()
    with closing(sqlite3.connect(store.params.sqlite_path)) as connection:
        names = {row[0] for row in connection.execute("SELECT config_key FROM chat_setting")}
    assert "onebot_v11_access_token" not in names


def test_startup_static_environment_overrides_legacy_database_token(
    tmp_path: Path, monkeypatch,
) -> None:
    """启动环境是 Token 的权威来源；旧数据库行和其他运行配置保持原样。"""
    store = _store(tmp_path)
    _legacy_token(store)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHAT_DB_BACKEND", "sqlite")
    monkeypatch.setenv("CHAT_DB_SQLITE_PATH", store.params.sqlite_path)
    monkeypatch.setenv("CHAT_SESSION_SECRET", config().chat_session_secret.get_secret_value())
    monkeypatch.setenv("ONEBOT_V11_ACCESS_TOKEN", NEW_TOKEN)
    loaded = load_startup_settings()
    assert loaded.config.onebot_v11_access_token.get_secret_value() == NEW_TOKEN
    assert loaded.revision == store.load_revision() == 1
    with closing(sqlite3.connect(store.params.sqlite_path)) as connection:
        token = connection.execute(
            "SELECT config_value FROM chat_setting WHERE config_key='onebot_v11_access_token'"
        ).fetchone()[0]
    assert json.loads(token) == "legacy-database-token-at-least-32-characters"


def test_startup_missing_static_token_does_not_use_database(
    tmp_path: Path, monkeypatch,
) -> None:
    """静态 Token 缺失时明确失败，不能借历史库行静默补回启动凭据。"""
    store = _store(tmp_path)
    _legacy_token(store)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHAT_DB_BACKEND", "sqlite")
    monkeypatch.setenv("CHAT_DB_SQLITE_PATH", store.params.sqlite_path)
    monkeypatch.setenv("CHAT_SESSION_SECRET", config().chat_session_secret.get_secret_value())
    monkeypatch.delenv("ONEBOT_V11_ACCESS_TOKEN", raising=False)
    with pytest.raises(ValueError, match="onebot_v11_access_token"):
        load_startup_settings()
    assert store.load_revision() == 1


def test_settings_init_keeps_token_in_static_file(tmp_path: Path, monkeypatch, capsys) -> None:
    """初始化只写动态字段；不生成数据库 Token，也不提供迁移 Token 的命令。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.sqlite3"))
    initialize_database(params)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ONEBOT_V11_ACCESS_TOKEN", NEW_TOKEN)
    env = tmp_path / ".env"
    env.write_text(
        f"CHAT_DB_BACKEND=sqlite\nCHAT_DB_SQLITE_PATH={params.sqlite_path}\n"
        f"CHAT_SESSION_SECRET={config().chat_session_secret.get_secret_value()}\n"
        f"ONEBOT_V11_ACCESS_TOKEN={NEW_TOKEN}\n", encoding="utf-8",
    )
    original = env.read_bytes()
    assert run(["init"], env_file=str(env)) == 0
    with closing(sqlite3.connect(params.sqlite_path)) as connection:
        names = {row[0] for row in connection.execute("SELECT config_key FROM chat_setting")}
    assert "onebot_v11_access_token" not in names
    assert env.read_bytes() == original
    assert run(["migrate-onebot-token"], env_file=str(env)) == 1
    assert NEW_TOKEN not in capsys.readouterr().out


def test_static_file_token_preserves_literal_characters(tmp_path: Path, monkeypatch) -> None:
    """dotenv 不展开认证字符，Token 与会话密钥仍是独立的静态秘密。"""
    monkeypatch.delenv("ONEBOT_V11_ACCESS_TOKEN", raising=False)
    literal = "literal-${UNCHANGED}-onebot-token-more-than-32-characters"
    env = tmp_path / ".env"
    env.write_text(
        f"ONEBOT_V11_ACCESS_TOKEN={literal}\n"
        f"CHAT_SESSION_SECRET={config().chat_session_secret.get_secret_value()}\n",
        encoding="utf-8",
    )
    loaded = load_static_config(env_file=str(env))
    assert loaded.onebot_v11_access_token.get_secret_value() == literal
    assert loaded.chat_session_secret != loaded.onebot_v11_access_token
