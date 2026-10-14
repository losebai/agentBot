"""插件管理的认证、配置版本和进程状态测试。"""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin


@pytest.fixture
async def plugin_client(admin_store, admin_runtime):
    """复用 Fake 数据库和公共管理认证，不启动 QQ。"""
    from tests.unit.test_plugins import FakePlugin

    from agent_bot.plugins.contracts import BuiltinPlugin, PluginMetadata
    from agent_bot.plugins.manager import PluginManager

    metadata = PluginMetadata("qqbot", "QQBot", "0.5.0", "离线 QQ 插件")
    manager = PluginManager([BuiltinPlugin(metadata, lambda: FakePlugin(metadata, []))], ["qqbot"])
    manager.prepare(SimpleNamespace())
    await manager.start()
    admin_runtime.plugins = manager
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token="test-admin"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline.test"
    ) as client:
        yield client
    await manager.stop()


async def test_plugin_list_requires_admin_authentication(plugin_client):
    """配置和内置能力目录同样受管理员边界保护。"""
    assert (await plugin_client.get("/api/v1/plugins")).status_code == 401


async def test_saved_selection_keeps_live_plugin_until_restart(plugin_client):
    """保存列表只改变持久配置和待重启展示。"""
    headers = {"Authorization": "Bearer test-admin"}
    response = await plugin_client.patch(
        "/api/v1/settings",
        headers=headers,
        json={
            "expected_revision": 1,
            "values": {"app_enabled_plugins": []},
        },
    )
    assert response.status_code == 200
    assert "app_enabled_plugins" in response.json()["restart_required_fields"]
    plugin = (await plugin_client.get("/api/v1/plugins", headers=headers)).json()[0]
    assert plugin["id"] == "qqbot"
    assert plugin["saved_enabled"] is False
    assert plugin["enabled"] is True
    assert plugin["status"] == "running"
    assert plugin["restart_required"] is True
    conflict = await plugin_client.patch(
        "/api/v1/settings",
        headers=headers,
        json={
            "expected_revision": 1,
            "values": {"app_enabled_plugins": ["qqbot"]},
        },
    )
    assert conflict.status_code == 409


async def test_invalid_plugin_selection_does_not_change_revision(plugin_client):
    """未知插件保存失败，原配置版本和选择均保留。"""
    headers = {"Authorization": "Bearer test-admin"}
    response = await plugin_client.patch(
        "/api/v1/settings",
        headers=headers,
        json={
            "expected_revision": 1,
            "values": {"app_enabled_plugins": ["unknown"]},
        },
    )
    assert response.status_code == 422
    settings = (await plugin_client.get("/api/v1/settings", headers=headers)).json()
    assert settings["database_revision"] == 1
