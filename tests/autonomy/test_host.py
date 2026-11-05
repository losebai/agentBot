"""独立HTTP宿主无需平台进程，认证、消息持久化和循环生命周期独立验收。"""

import asyncio

import httpx

from agent_bot.admin.config import AdminConfig
from agent_bot.autonomy import host
from agent_bot.config import RuntimeConfig
from agent_bot.settings import SettingsStore


async def test_host_starts_local_worker_and_chat_api_without_bot(store, monkeypatch):
    """隔离库启动真实HTTP路由，后台替身只等待事件，不连接QQ或访问付费模型。"""
    config = RuntimeConfig(onebot_v11_access_token='o' * 32, chat_session_secret='s' * 32,
                           app_enabled_plugins=[])
    settings = SettingsStore(store.params, connect=store.connect, static_config=config)
    stored = settings.initialize(config)
    monkeypatch.setattr(host, 'load_startup_settings', lambda: stored)
    monkeypatch.setattr(host, 'load_bootstrap_config', lambda: object())
    monkeypatch.setattr(host, 'database_params', lambda config: store.params)
    monkeypatch.setattr(host.AdminConfig, 'from_environment',
                        lambda: AdminConfig(token='offline-password'))
    stopped = asyncio.Event()

    async def worker(*, ready, local_only):
        """必须显式只处理本地身份，退出时响应宿主取消。"""
        assert local_only is True
        ready.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(host, 'serve', worker)
    app = host.create_host()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url='http://offline',
        ) as api:
            assert (await api.get('/api/v1/autonomy/profiles')).status_code == 401
            assert (await api.get('/')).headers['location'] == '/index.html?standalone=1'
            assert (await api.get('/health/ready')).status_code == 200
            headers = {'Authorization': 'Bearer offline-password'}
            profile = (await api.post('/api/v1/autonomy/profiles', headers=headers, json={
                'owner': {'channel': 'local', 'agent_key': 'alice', 'participant_id': 'human',
                          'session_id': 'local'}, 'identity': {'name': 'Alice'},
            })).json()
            legacy = await api.post('/api/v1/autonomy/profiles', headers=headers, json={
                'owner': {'bot_id': '10001', 'sender_id': '10002', 'session_id': 'qq',
                          'scene': 'private'}, 'identity': {'name': 'QQ'},
            })
            assert legacy.status_code == 409
            assert profile['owner']['channel'] == 'local'
            assert 'draft' in profile['policy']['allowed_actions']
            path = '/api/v1/autonomy/profiles/' + profile['id']
            sent = await api.post(path + '/messages', headers=headers,
                                  json={'request_id': 'one', 'text': '你好'})
            assert sent.status_code == 201 and sent.json()['status'] == 'pending'
            bad = await api.post(path + '/messages', headers=headers,
                                 json={'request_id': 'two', 'text': 'secret-test', 'unknown': True})
            assert bad.status_code == 422 and 'secret-test' not in bad.text
            assert (await api.get(path + '/conversations', headers=headers)).json()['items']
            assert (await api.get('/api/v1/qq/status', headers=headers)).status_code == 404
    assert stopped.is_set()
