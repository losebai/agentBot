"""QQ 插件仅能操作所属桌面 NapCat，沿用认证并拒绝跨站和远程控制。"""

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_napcat_status_is_authenticated_and_read_only(admin_store, admin_runtime):
    """匿名请求失败；公开状态不含命令、文件或秘密。"""
    from agent_bot.admin.config import AdminConfig
    from agent_bot.admin.routes import install_admin

    app = FastAPI()
    install_admin(app, admin_store, admin_runtime,
                  AdminConfig(token="offline-admin-password"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test",
    ) as client:
        assert (await client.get("/api/v1/napcat/status")).status_code == 401
        headers = {"Authorization": "Bearer offline-admin-password"}
        response = await client.get("/api/v1/napcat/status", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"mode": "external", "state": "not_started",
                                   "webui_ready": False, "error_code": None}
        assert (await client.post("/api/v1/napcat/status", headers=headers)).status_code == 405
        assert (await client.post("/api/v1/napcat/start", headers=headers)).status_code == 403


@pytest.mark.asyncio
async def test_plugin_start_and_login_use_only_owned_desktop_context(admin_store, admin_runtime):
    """命令没有路径或端口参数，登录请求只通知所属桌面窗口。"""
    from types import SimpleNamespace
    from unittest.mock import Mock

    from agent_bot.admin.config import AdminConfig
    from agent_bot.admin.routes import install_admin
    from agent_bot.napcat.contracts import NapCatStatus

    supervisor = SimpleNamespace(
        config=SimpleNamespace(app_enabled_plugins=["qqbot"]),
        snapshot=Mock(return_value=NapCatStatus("managed", "running", True)),
        start=Mock(),
    )
    login = Mock()
    app = FastAPI()
    app.state.napcat = supervisor
    app.state.napcat_backend_url = "http://127.0.0.1:8080/"
    app.state.napcat_open_login = login
    install_admin(app, admin_store, admin_runtime, AdminConfig(token="offline-admin"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url="http://127.0.0.1:8080") as client:
        headers = {"Authorization": "Bearer offline-admin", "Origin": "http://127.0.0.1:8080"}
        assert (await client.post("/api/v1/napcat/start")).status_code == 401
        assert (await client.post("/api/v1/napcat/start", headers=headers | {
            "Origin": "https://foreign.example",
        })).status_code == 403
        assert not supervisor.start.called
        response = await client.post("/api/v1/napcat/start", headers=headers, json={})
        assert response.status_code == 200
        supervisor.start.assert_called_once_with("http://127.0.0.1:8080/", managed=True)
        response = await client.post("/api/v1/napcat/login", headers=headers, json={})
        assert response.status_code == 200
        login.assert_called_once_with()
        supervisor.snapshot.return_value = NapCatStatus(
            "managed", "failed", False, "PROCESS_EXITED",
        )
        response = await client.post("/api/v1/napcat/login", headers=headers, json={})
        assert response.status_code == 409
        supervisor.config.app_enabled_plugins = []
        response = await client.post("/api/v1/napcat/start", headers=headers, json={})
        assert response.status_code == 409
        assert supervisor.start.call_count == 1


@pytest.mark.asyncio
async def test_plain_service_and_remote_client_cannot_control_processes(admin_store, admin_runtime):
    """普通 CLI 没有托管控制上下文，远程访问不能调用本机窗口能力。"""
    from agent_bot.admin.config import AdminConfig
    from agent_bot.admin.routes import install_admin

    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url="http://127.0.0.1:8080") as client:
        response = await client.get("/api/v1/napcat/plugin")
        assert response.json()["available"] is False
        assert (await client.post("/api/v1/napcat/start", json={}, headers={
            "Origin": "http://127.0.0.1:8080",
        })).status_code == 409
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=("192.0.2.1", 123)),
                                 base_url="http://127.0.0.1:8080") as client:
        assert (await client.post("/api/v1/napcat/start", json={}, headers={
            "Origin": "http://127.0.0.1:8080",
        })).status_code == 403
