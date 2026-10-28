"""B 站管理端点复用已有认证，不隐式启用插件。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from agent_bot.admin.agent_chat import AgentChatProbe, AgentProbeRequest
from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin


def test_video_reference_does_not_consume_public_prompt_length():
    """完整 2000 字用户输入可以附视频，内部资产说明由服务端构造。"""
    body = AgentProbeRequest(agent_code='helper', prompt='文' * 2000, session_id='s',
                             video_asset_id='a' * 32)
    probe = AgentChatProbe(SimpleNamespace(config=SimpleNamespace(chat_timeout_seconds=300)))
    request = probe.request(body, 'offline')
    assert request.prompt.startswith('文' * 2000)
    assert 'a' * 32 in request.prompt
    assert request.attachment_ids == ()


async def test_bilibili_routes_require_auth_and_running_plugin(admin_store, admin_runtime):
    """没有认证不能生成二维码，禁用插件不能通过 API 自行启动。"""
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token='offline-admin'))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://offline.test') as client:
        assert (await client.post('/api/v1/bilibili/login/qr')).status_code == 401
        headers = {'Authorization': 'Bearer offline-admin'}
        assert (await client.post('/api/v1/bilibili/login/qr', headers=headers)).status_code == 503
        service = SimpleNamespace(generate_qr=AsyncMock(return_value={
            'session_id': 'opaque', 'url': 'https://passport.bilibili.com/test',
            'status': 'waiting', 'expires_at': 100,
        }))
        admin_runtime.plugins = SimpleNamespace(running_plugin=lambda id: SimpleNamespace(
            service=service))
        response = await client.post('/api/v1/bilibili/login/qr', headers=headers)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert 'qrcode_key' not in response.json()


async def test_dynamic_http_route_requires_auth_runtime_and_scoped_assets(
    admin_store, admin_runtime,
):
    """HTTP 发布继承管理认证，显式请求 ID 与上传会话用于防重和媒体授权。"""
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token='offline-admin'))
    body = {'text': '今天的照片', 'image_asset_ids': ['a' * 32],
            'session_id': 'dynamic-test', 'request_id': 'stable-dynamic-request'}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url='http://offline.test') as client:
        path = '/api/v1/bilibili/dynamics'
        assert (await client.post(path, json=body)).status_code == 401
        headers = {'Authorization': 'Bearer offline-admin'}
        assert (await client.post(path, json=body, headers=headers)).status_code == 503
        service = SimpleNamespace(write=AsyncMock(return_value={
            'dynamic_id': '123', 'url': 'https://t.bilibili.com/123', 'status': 'submitted',
        }))
        admin_runtime.plugins = SimpleNamespace(running_plugin=lambda id: SimpleNamespace(
            service=service))
        media_store = object()
        admin_runtime.media_resources = SimpleNamespace(
            store=media_store, require_ready=lambda: None,
        )
        response = await client.post(path, json=body, headers=headers)
        assert response.status_code == 201, response.text
        assert response.headers['cache-control'] == 'no-store'
        args, kwargs = service.write.call_args
        assert args[0] == 'bilibili_publish_dynamic'
        assert args[1].image_asset_ids == ['a' * 32]
        assert kwargs['owner'].entry == 'admin'
        assert kwargs['owner'].session_id == 'admin_dynamic-test'
        assert kwargs['request_id'] == 'stable-dynamic-request'
        assert kwargs['media_store'] is media_store
        service.write.reset_mock()
        invalid = await client.post(path, json={**body, 'uid': '999'}, headers=headers)
        assert invalid.status_code == 422
        missing_id = {key: value for key, value in body.items() if key != 'request_id'}
        assert (await client.post(path, json=missing_id, headers=headers)).status_code == 422
        service.write.assert_not_awaited()
