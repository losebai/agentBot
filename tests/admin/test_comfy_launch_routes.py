"""Comfy 本机启动只传达受认证和同源约束的启动意图。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_comfy_launch_rejects_remote_origin_and_arguments():
    """路径和启动参数不能从 HTTP 请求传入，也不能跨站发起启动。"""
    from agent_bot.admin.comfy_routes import install_comfy_routes

    state = {"state": "online", "configured": True}
    launcher = SimpleNamespace(snapshot=AsyncMock(return_value=state),
                               start=AsyncMock(return_value=state))
    runtime = SimpleNamespace(comfy_launcher=launcher, config=SimpleNamespace(
        chat_local_workflow_base_url="http://127.0.0.1:8188"))
    app = FastAPI()
    from fastapi import APIRouter

    router = APIRouter()
    install_comfy_routes(router, runtime)
    app.include_router(router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url="http://127.0.0.1:8080") as client:
        assert (await client.post('/comfy/start', json={})).status_code == 403
        headers = {"Origin": "http://127.0.0.1:8080"}
        assert (await client.post('/comfy/start', headers=headers,
                                  json={"command": "arbitrary"})).status_code == 422
        assert (await client.post('/comfy/start?path=x', headers=headers,
                                  json={})).status_code == 422
        launcher.start.assert_not_awaited()
        assert (await client.post('/comfy/start', headers=headers, json={})).status_code == 200
        launcher.start.assert_awaited_once_with("http://127.0.0.1:8188")
        runtime.comfy_launcher = None
        assert (await client.post('/comfy/start', headers=headers, json={})).status_code == 409


def test_comfy_launch_config_is_static_and_checks_reserved_arguments():
    """解释器、目录和参数属于启动配置，附加参数不能覆盖监听目标。"""
    from pydantic import ValidationError

    from agent_bot.static_config import STATIC_SETTING_NAMES, StaticConfig

    names = {'agent_bot_comfy_python', 'agent_bot_comfy_directory', 'agent_bot_comfy_args'}
    assert names <= STATIC_SETTING_NAMES
    base = {'onebot_v11_access_token': 'a' * 32, 'chat_session_secret': 'b' * 32}
    value = StaticConfig.model_validate(base | {'agent_bot_comfy_args': '["--cpu"]'})
    assert value.agent_bot_comfy_args == ['--cpu']
    with pytest.raises(ValidationError):
        StaticConfig.model_validate(base | {'agent_bot_comfy_args': '["--port=9000"]'})
    with pytest.raises(ValidationError):
        StaticConfig.model_validate(base | {'agent_bot_comfy_args': '["--lis=0.0.0.0"]'})


@pytest.mark.asyncio
async def test_comfy_launch_inherits_authentication(admin_store, admin_runtime):
    """状态及启动接口共用管理员认证，远程请求不能启动本机程序。"""
    from agent_bot.admin.config import AdminConfig
    from agent_bot.admin.routes import install_admin

    launcher = SimpleNamespace(snapshot=AsyncMock(return_value={"state": "online"}),
                               start=AsyncMock())
    admin_runtime.comfy_launcher = launcher
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token="offline-admin"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                 base_url="http://127.0.0.1:8080") as client:
        assert (await client.get('/api/v1/comfy/status')).status_code == 401
        assert (await client.post('/api/v1/comfy/start', json={})).status_code == 401
        assert (await client.get('/api/v1/comfy/status', headers={
            'Authorization': 'Bearer offline-admin',
        })).status_code == 200
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app, client=('192.0.2.1', 123)),
                                 base_url="http://127.0.0.1:8080") as client:
        assert (await client.post('/api/v1/comfy/start', json={}, headers={
            'Authorization': 'Bearer offline-admin', 'Origin': 'http://127.0.0.1:8080',
        })).status_code == 403
    launcher.start.assert_not_awaited()
