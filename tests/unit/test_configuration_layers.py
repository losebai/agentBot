"""配置分层在真实 SQLite 文件和管理契约上的可观察行为。"""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pymysql
import pytest
from pydantic import ValidationError

from agent_bot.admin.models import SettingsUpdate
from agent_bot.config import RuntimeConfig
from agent_bot.database import connect_database, initialize_database
from agent_bot.settings import DatabaseParams, SettingsRevisionConflictError, SettingsStore


def config(**values) -> RuntimeConfig:
    """使用不同的测试密钥构造有效快照。"""
    return RuntimeConfig(
        onebot_v11_access_token="offline-onebot-token-more-than-thirty-two-characters",
        chat_session_secret="offline-session-secret-more-than-thirty-two-characters",
        **values,
    )


def test_persist_only_dynamic_settings_and_ignore_legacy_static_rows(tmp_path: Path) -> None:
    """历史静态值不能覆盖启动值，动态配置仍由数据库决定。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.db"))
    initialize_database(params)
    store = SettingsStore(params, static_config=config(port=8765))
    store.initialize(config(port=8765, chat_model="saved-model"))
    with closing(connect_database(params, cursorclass=pymysql.cursors.DictCursor)) as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT config_key FROM chat_setting")
            names = {row["config_key"] for row in cursor.fetchall()}
            assert "chat_model" in names
            assert "port" not in names
            assert "onebot_v11_access_token" not in names
            cursor.execute(
                "INSERT INTO chat_setting (config_key,config_value) VALUES (%s,%s)",
                ("port", "9999"),
            )
        db.commit()
    loaded = store.load()
    assert loaded.config.port == 8765
    assert loaded.config.chat_model == "saved-model"


def test_static_api_update_is_rejected_before_persistence() -> None:
    """仅界面只读不足以保护启动参数，API 必须拒绝静态字段。"""
    with pytest.raises(ValidationError):
        SettingsUpdate(expected_revision=1, values={"port": 9999})
    with pytest.raises(ValidationError):
        SettingsUpdate(expected_revision=1, values={"onebot_v11_access_token": "replacement"})


def test_database_choice_is_visible_as_read_only_static_configuration(tmp_path: Path) -> None:
    """管理员能看见实际所选后端，但不能把切换数据库提交为运行更新。"""
    from types import SimpleNamespace

    from agent_bot.admin.settings import SettingsService

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.db"))
    initialize_database(params)
    initial = config()
    store = SettingsStore(params, static_config=initial)
    store.initialize(initial)
    snapshot = SettingsService(store, SimpleNamespace(config=initial, revision=1)).read()
    backend = next(field for field in snapshot.fields if field.name == "chat_db_backend")
    assert backend.value == "sqlite" and backend.scope == "static" and backend.read_only
    with pytest.raises(ValidationError):
        SettingsUpdate(expected_revision=1, values={"chat_db_backend": "mysql"})


def test_cli_initializes_defaults_without_importing_dynamic_environment(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """新安装命令从静态启动文件初始化，环境中的业务值不会被自动导入。"""
    from agent_bot.__main__ import main
    from agent_bot.config_cli import run

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHAT_DB_BACKEND", "sqlite")
    monkeypatch.setenv("CHAT_DB_SQLITE_PATH", str(tmp_path / "bot.db"))
    monkeypatch.setenv(
        "ONEBOT_V11_ACCESS_TOKEN", config().onebot_v11_access_token.get_secret_value()
    )
    monkeypatch.setenv("CHAT_SESSION_SECRET", config().chat_session_secret.get_secret_value())
    monkeypatch.setenv("CHAT_MODEL", "must-not-import")
    assert main(["database", "init"]) == 0
    assert run(["init"]) == 0
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.db"))
    assert SettingsStore(params, static_config=config()).load().config.chat_model == "Qwen/Qwen3-8B"
    assert run(["init"]) == 1
    assert "offline-onebot-token" not in capsys.readouterr().out


def test_concurrent_revision_writers_do_not_overwrite_each_other(tmp_path: Path) -> None:
    """两个连接使用同一旧版本提交时，只允许一次成功。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.db"))
    initialize_database(params)
    store = SettingsStore(params, static_config=config())
    store.initialize(config())

    def save(model: str) -> str:
        """每次操作独立事务，不共享游标。"""
        candidate = config(chat_model=model)
        try:
            store.save(candidate, expected_revision=1)
            return "saved"
        except SettingsRevisionConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, ("first", "second")))
    assert sorted(results) == ["conflict", "saved"]
    assert store.load().revision == 2


def test_startup_static_environment_and_database_dynamic_values(
    tmp_path: Path, monkeypatch
) -> None:
    """启动只读取静态环境，动态环境不能覆盖已保存模型。"""
    from agent_bot.bootstrap import load_startup_settings

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.db"))
    initialize_database(params)
    SettingsStore(params, static_config=config()).initialize(config(chat_model="saved-model"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHAT_DB_BACKEND", "sqlite")
    monkeypatch.setenv("CHAT_DB_SQLITE_PATH", params.sqlite_path)
    monkeypatch.setenv(
        "ONEBOT_V11_ACCESS_TOKEN", config().onebot_v11_access_token.get_secret_value()
    )
    monkeypatch.setenv("CHAT_SESSION_SECRET", config().chat_session_secret.get_secret_value())
    monkeypatch.setenv("PORT", "8765")
    monkeypatch.setenv("CHAT_MODEL", "environment-model")
    loaded = load_startup_settings()
    assert loaded.config.port == 8765
    assert loaded.config.chat_model == "saved-model"


def test_onebot_connection_addresses_load_from_database_not_environment(
    tmp_path: Path, monkeypatch,
) -> None:
    """正向 WS 和 HTTP 地址由数据库保存，旧环境中的空地址不能清除已保存连接。"""
    from agent_bot.bootstrap import load_startup_settings

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.db"))
    initialize_database(params)
    initial = config(
        onebot_v11_ws_urls={"ws://onebot.test:3001/"},
        onebot_v11_api_roots={"123456": "http://onebot.test:3000/"},
    )
    SettingsStore(params, static_config=config()).initialize(initial)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CHAT_DB_BACKEND", "sqlite")
    monkeypatch.setenv("CHAT_DB_SQLITE_PATH", params.sqlite_path)
    token = initial.onebot_v11_access_token.get_secret_value()
    monkeypatch.setenv("ONEBOT_V11_ACCESS_TOKEN", token)
    monkeypatch.setenv("CHAT_SESSION_SECRET", initial.chat_session_secret.get_secret_value())
    monkeypatch.setenv("ONEBOT_V11_WS_URLS", "[]")
    monkeypatch.setenv("ONEBOT_V11_API_ROOTS", "{}")

    loaded = load_startup_settings()

    assert loaded.config.onebot_v11_ws_urls == initial.onebot_v11_ws_urls
    assert loaded.config.onebot_v11_api_roots == initial.onebot_v11_api_roots
    assert loaded.config.onebot_v11_access_token.get_secret_value() == token


def test_onebot_address_updates_are_editable_but_require_restart(tmp_path: Path) -> None:
    """接入地址可在数据库中修改，运行中的适配器连接必须明确标记待重启。"""
    from types import SimpleNamespace

    from agent_bot.admin.settings import SettingsService

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.db"))
    initialize_database(params)
    initial = config()
    store = SettingsStore(params, static_config=initial)
    store.initialize(initial)
    manager = SettingsService(store, SimpleNamespace(config=initial, revision=1))
    request = SettingsUpdate(
        expected_revision=1, values={"onebot_v11_ws_urls": ["ws://onebot.test:3001/"]},
    )

    snapshot = manager.update(request)

    field = next(item for item in snapshot.fields if item.name == "onebot_v11_ws_urls")
    assert field.scope == "runtime" and not field.read_only and field.restart_required
    assert snapshot.restart_required_fields == ["onebot_v11_ws_urls"]
    assert str(next(iter(store.load().config.onebot_v11_ws_urls))) == "ws://onebot.test:3001/"
    assert not initial.onebot_v11_ws_urls


def test_export_static_is_explicit_secret_safe_and_refuses_overwrite(
    tmp_path: Path, capsys
) -> None:
    """停服迁移导出原始静态行，终端不打印秘密且不覆盖目标文件。"""
    from agent_bot.config_cli import run

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.db"))
    initialize_database(params)
    with closing(connect_database(params)) as db:
        with db.cursor() as cursor:
            cursor.execute(
                "INSERT INTO chat_setting (config_key,config_value) VALUES (%s,%s)",
                ("onebot_v11_access_token", json.dumps("old-secret-do-not-print")),
            )
        db.commit()
    env = tmp_path / "bootstrap.env"
    env.write_text(
        f"CHAT_DB_BACKEND=sqlite\nCHAT_DB_SQLITE_PATH={params.sqlite_path}\n", encoding="utf-8"
    )
    target = tmp_path / "static.env"
    assert run(["export-static", str(target)], env_file=str(env)) == 0
    assert "old-secret-do-not-print" in target.read_text(encoding="utf-8")
    assert "old-secret-do-not-print" not in capsys.readouterr().out
    target.write_text("preserved", encoding="utf-8")
    assert run(["export-static", str(target)], env_file=str(env)) == 1
    assert target.read_text(encoding="utf-8") == "preserved"


def test_export_roundtrip_preserves_backslashes_and_literal_secret(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    """迁移后的命令字符与会话密钥不能被 dotenv 转义或插值改变。"""
    from agent_bot.bootstrap import load_legacy_runtime_config, load_static_config
    from agent_bot.config_cli import run

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.db"))
    initialize_database(params)
    secret = "offline-literal-${UNSET_REVIEW_VAR}-session-secret\x01\r\n\b\f\t\v-end"
    monkeypatch.setenv("REVIEW_LITERAL", 'unescaped "quote"')
    values = {
        "chat_session_secret": secret,
        "onebot_v11_access_token": config().onebot_v11_access_token.get_secret_value(),
        "command_start": ["\\", "C:\\bots", "O'Reilly"],
        "superusers": ["${REVIEW_LITERAL}"],
        "onebot_v11_secret": None,
        "chat_max_input_chars": "not-json",
    }
    with closing(connect_database(params)) as db:
        with db.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO chat_setting (config_key,config_value) VALUES (%s,%s)",
                [
                    (key, value if key == "chat_max_input_chars" else json.dumps(value))
                    for key, value in values.items()
                ],
            )
        db.commit()
    env = tmp_path / "bootstrap.env"
    env.write_text(
        f"CHAT_DB_BACKEND=sqlite\nCHAT_DB_SQLITE_PATH={params.sqlite_path}\n", encoding="utf-8"
    )
    target = tmp_path / "export.env"
    assert run(["export-static", str(target)], env_file=str(env)) == 0
    loaded = load_static_config(env_file=str(target))
    assert loaded.command_start == {"\\", "C:\\bots", "O'Reilly"}
    assert loaded.chat_session_secret.get_secret_value() == secret
    assert loaded.superusers == {"${REVIEW_LITERAL}"}
    assert loaded.onebot_v11_secret is None
    # 显式旧环境导入也必须使用静态秘密字面值，不能在整体验证前改写它。
    imported = load_legacy_runtime_config(env_file=str(target))
    assert imported.chat_session_secret.get_secret_value() == secret
    assert secret not in capsys.readouterr().out
