import os
from unittest.mock import Mock

import nonebot
import pytest

from agent_bot.bootstrap import nonebot_init_kwargs
from agent_bot.bot_cli import main, run_bot
from agent_bot.config import RuntimeConfig


def runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        onebot_v11_access_token="database-onebot-token-that-is-at-least-32-characters",
        dispatch_chat_session_secret="database-session-secret-that-is-at-least-32-characters",
    )


def test_run_bot_loads_database_before_nonebot_and_adapter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    driver = Mock()
    driver.register_adapter.side_effect = lambda _adapter: calls.append("adapter")
    config = runtime_config()

    monkeypatch.setattr(
        "agent_bot.bot_cli.load_startup_settings",
        lambda: calls.append("database") or Mock(config=config, revision=7),
    )
    monkeypatch.setattr(
        "agent_bot.bot_cli.scrub_runtime_environment",
        lambda: calls.append("scrub"),
    )
    monkeypatch.setattr(nonebot, "init", lambda **_kwargs: calls.append("nonebot"))
    monkeypatch.setattr(nonebot, "get_driver", Mock(return_value=driver))
    monkeypatch.setattr(
        "agent_bot.bot_cli.inject_runtime_config",
        lambda _global, _runtime, *, revision: calls.append(f"inject:{revision}"),
    )
    monkeypatch.setattr(
        nonebot,
        "load_plugin",
        lambda _name: calls.append("plugin") or object(),
    )
    monkeypatch.setattr(nonebot, "run", lambda: calls.append("run"))

    run_bot()

    assert calls == [
        "database",
        "scrub",
        "nonebot",
        "inject:7",
        "adapter",
        "plugin",
        "run",
    ]


def test_run_bot_injects_database_config_without_loading_legacy_dotenv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = runtime_config()
    init = Mock()
    driver = Mock()
    monkeypatch.setattr(
        "agent_bot.bot_cli.load_startup_settings",
        Mock(return_value=Mock(config=config, revision=1)),
    )
    scrub = Mock()
    inject = Mock()
    monkeypatch.setattr("agent_bot.bot_cli.scrub_runtime_environment", scrub)
    monkeypatch.setattr("agent_bot.bot_cli.inject_runtime_config", inject)
    monkeypatch.setattr(nonebot, "init", init)
    monkeypatch.setattr(nonebot, "get_driver", Mock(return_value=driver))
    monkeypatch.setattr(nonebot, "load_plugin", Mock(return_value=object()))
    monkeypatch.setattr(nonebot, "run", Mock())

    run_bot()

    expected = nonebot_init_kwargs(config)
    init.assert_called_once()
    args, kwargs = init.call_args
    assert args == ()
    assert kwargs.pop("_env_file") == (os.devnull,)
    assert kwargs == expected
    scrub.assert_called_once_with()
    inject.assert_called_once_with(driver.config, config, revision=1)


def test_run_bot_fails_closed_when_database_load_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init = Mock()
    run = Mock()
    monkeypatch.setattr(
        "agent_bot.bot_cli.load_startup_settings",
        Mock(side_effect=RuntimeError("settings database unavailable")),
    )
    monkeypatch.setattr(nonebot, "init", init)
    monkeypatch.setattr(nonebot, "run", run)

    with pytest.raises(RuntimeError, match="settings database unavailable"):
        run_bot()

    init.assert_not_called()
    run.assert_not_called()


def test_run_bot_fails_fast_when_plugin_cannot_load(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Mock()
    run = Mock()
    monkeypatch.setattr(
        "agent_bot.bot_cli.load_startup_settings",
        Mock(return_value=Mock(config=runtime_config(), revision=1)),
    )
    monkeypatch.setattr(nonebot, "init", Mock())
    monkeypatch.setattr(nonebot, "get_driver", Mock(return_value=driver))
    monkeypatch.setattr(nonebot, "load_plugin", Mock(return_value=None))
    monkeypatch.setattr(nonebot, "run", run)

    with pytest.raises(RuntimeError, match=r"agent_bot\.plugin"):
        run_bot()

    driver.register_adapter.assert_called_once()
    run.assert_not_called()


def test_run_bot_runs_after_plugin_loads(monkeypatch: pytest.MonkeyPatch) -> None:
    driver = Mock()
    run = Mock()
    monkeypatch.setattr(
        "agent_bot.bot_cli.load_startup_settings",
        Mock(return_value=Mock(config=runtime_config(), revision=1)),
    )
    monkeypatch.setattr(nonebot, "init", Mock())
    monkeypatch.setattr(nonebot, "get_driver", Mock(return_value=driver))
    monkeypatch.setattr(nonebot, "load_plugin", Mock(return_value=object()))
    monkeypatch.setattr(nonebot, "run", run)

    run_bot()

    driver.register_adapter.assert_called_once()
    run.assert_called_once_with()


def test_main_without_arguments_runs_bot(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock(return_value=None)
    monkeypatch.setattr("agent_bot.bot_cli.run_bot", run)

    assert main([]) == 0

    run.assert_called_once_with()


def test_main_dispatches_workflow_admin_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    from agent_bot import workflow_cli

    run = Mock(return_value=0)
    monkeypatch.setattr(workflow_cli, "run", run)

    assert main(["workflows", "sync", "C:/safe/workflows"]) == 0

    run.assert_called_once_with(["sync", "C:/safe/workflows"])
