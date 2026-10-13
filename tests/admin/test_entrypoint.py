"""Validate CLI dispatch and safe failure before a real bot process is started."""

import subprocess
import sys
from unittest.mock import Mock

import pytest


def test_help_does_not_load_database_or_start_bot(capsys):
    from agent_bot.__main__ import main

    assert main(["--help"]) == 0
    assert "agent-bot" in capsys.readouterr().out


def test_settings_command_uses_existing_offline_cli(monkeypatch):
    from agent_bot import config_cli
    from agent_bot.__main__ import main

    command = Mock(return_value=0)
    monkeypatch.setattr(config_cli, "run", command)
    assert main(["settings", "list"]) == 0
    command.assert_called_once_with(["list"])


def test_unknown_command_does_not_start_service():
    from agent_bot.__main__ import main

    with pytest.raises(SystemExit):
        main(["unknown-command"])


def test_missing_admin_credential_fails_before_database(monkeypatch, capsys):
    """缺失管理员凭据时给出操作提示，并在数据库访问前返回失败。"""
    from agent_bot.__main__ import main
    from agent_bot.admin.config import AdminConfig

    monkeypatch.setattr(AdminConfig, "from_environment", Mock(side_effect=ValueError("missing")))
    assert main([]) == 1
    assert "AGENT_BOT_ADMIN_TOKEN" in capsys.readouterr().out


def test_non_fastapi_driver_is_rejected(admin_runtime):
    from agent_bot.admin.config import AdminConfig
    from agent_bot.application import create_application
    from agent_bot.settings import VersionedSettings

    config = admin_runtime.config.model_copy(update={"driver": "~none"})
    with pytest.raises(ValueError, match="FastAPI"):
        create_application(VersionedSettings(config, 1), AdminConfig(token="x" * 48))


def test_application_import_does_not_initialize_plugin_or_open_database():
    """A clean process must be able to import the application before NoneBot initialization."""
    code = (
        "try:\n import agent_bot.application\n"
        "except Exception as error:\n print(type(error).__name__); raise SystemExit(1)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"Application import caused startup side effects: {result.stdout}"
    )


async def test_application_assembles_management_and_preserves_existing_routes(
    monkeypatch, admin_store, admin_runtime
):
    """Exercise successful assembly against a real FastAPI app and isolated dependencies."""
    from types import SimpleNamespace

    import httpx
    import nonebot
    from fastapi import FastAPI

    from agent_bot.admin.config import AdminConfig
    from agent_bot.application import create_application
    from agent_bot.plugin import application as plugin
    from agent_bot.settings import VersionedSettings

    app = FastAPI()

    @app.get("/existing-route")
    async def original():
        return {"preserved": True}

    driver = SimpleNamespace(config=SimpleNamespace(), server_app=app, register_adapter=Mock())
    monkeypatch.setattr(nonebot, "init", Mock())
    monkeypatch.setattr(nonebot, "get_driver", lambda: driver)
    monkeypatch.setattr(nonebot, "load_plugin", lambda _: object())
    monkeypatch.setattr(plugin, "get_settings_store", lambda: admin_store)
    token = "offline-admin-token-longer-than-thirty-two-characters"
    result = create_application(
        VersionedSettings(admin_runtime.config, 1), AdminConfig(token=token)
    )
    assert result is app
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline.test"
    ) as client:
        assert (await client.get("/existing-route")).json() == {"preserved": True}
        assert (await client.get("/health/live")).status_code == 200
        response = await client.get(
            "/api/v1/settings", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert "offline-ai-secret" not in response.text
