"""持续身份管理使用临时SQLite验收认证、幂等、版本与跨身份隔离。"""

import json
import sqlite3
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.settings import DatabaseParams

TOKEN = "offline-autonomy-admin-password"
ROOT = Path(__file__).resolve().parents[2]
HEADERS = {"Authorization": "Bearer " + TOKEN}
BODY = {
    "owner": {"bot_id": "10001", "sender_id": "10002", "session_id": "offline", "scene": "private"},
    "identity": {"name": "持续身份"},
}


@pytest.fixture
async def autonomy_api(tmp_path, admin_store, admin_runtime):
    """显式从仓库初始化隔离库，避免读取开发机器旧editable包的SQL资源。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "autonomy.db"))
    script = ROOT / "sql/agent_bot_init.sqlite.sql"
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.executescript(script.read_text("utf-8"))
    admin_runtime.tagged_memory_store = TaggedMemoryStore(
        params, secret=admin_runtime.config.chat_session_secret.get_secret_value()
    )
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=TOKEN))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    ) as client:
        yield client


async def test_autonomy_requires_admin_and_rejects_unknown_permissions(autonomy_api):
    """管理认证覆盖读取、写入、暂停和评估；任意工具不能混入动作白名单。"""
    api = autonomy_api
    for method, path in (
        ("GET", "/profiles"),
        ("GET", "/profiles/unknown/conversations"),
        ("POST", "/profiles"),
        ("POST", "/pause"),
        ("POST", "/profiles/unknown/evaluate"),
    ):
        response = await api.request(method, "/api/v1/autonomy" + path)
        assert response.status_code == 401
    bad = await api.post(
        "/api/v1/autonomy/profiles",
        headers=HEADERS,
        json={**BODY, "policy": {"allowed_actions": ["shell"]}},
    )
    assert bad.status_code == 422
    assert "shell" not in bad.text


async def test_profile_idempotency_revision_and_cross_profile_task_isolation(autonomy_api):
    """重复认领不覆盖设定，陈旧编辑明确冲突，另一身份不能审核本身份的任务。"""
    api = autonomy_api
    root = "/api/v1/autonomy/profiles"
    created = await api.post(root, headers=HEADERS, json=BODY)
    assert created.status_code == 201
    first = created.json()
    assert first["policy"]["paused"] is True
    again = await api.post(root, headers=HEADERS, json={**BODY, "identity": {"name": "不得覆盖"}})
    assert again.json() == first
    path = root + "/" + first["id"]
    patched = await api.patch(
        path, headers=HEADERS, json={"expected_revision": 1, "identity": {"name": "已确认新名称"}}
    )
    assert patched.status_code == 200 and patched.json()["revision"] == 2
    stale = await api.patch(
        path, headers=HEADERS, json={"expected_revision": 1, "identity": {"name": "陈旧覆盖"}}
    )
    assert stale.status_code == 409
    task = await api.post(
        path + "/tasks",
        headers=HEADERS,
        json={"request_id": "same-request", "goal": "等待重启后继续"},
    )
    assert task.status_code == 201
    other = await api.post(
        root, headers=HEADERS, json={**BODY, "owner": {**BODY["owner"], "sender_id": "10003"}}
    )
    foreign = await api.post(
        root + "/" + other.json()["id"] + "/tasks/" + task.json()["id"] + "/decision",
        headers=HEADERS,
        json={"expected_revision": 1, "approve": False},
    )
    assert foreign.status_code == 404
    assert (await api.get(path + "/tasks", headers=HEADERS)).json()[0]["status"] == "pending"
    assert (await api.post("/api/v1/autonomy/pause", headers=HEADERS)).status_code == 204
    assert all(row["policy"]["paused"] for row in (await api.get(root, headers=HEADERS)).json())


async def test_event_source_validation_and_audit_are_persistent(autonomy_api):
    """第三方资料不能请求任务，入站原文以幂等事件保存且审计可追踪。"""
    api = autonomy_api
    profile = (await api.post("/api/v1/autonomy/profiles", headers=HEADERS, json=BODY)).json()
    path = "/api/v1/autonomy/profiles/" + profile["id"]
    event = {"event_id": "event", "text": "整理部署资料", "occurred_at": "2026-11-03T04:00:00Z"}
    bad = await api.post(
        path + "/events",
        headers=HEADERS,
        json={**event, "task_requested": True, "source": "third_party"},
    )
    assert bad.status_code == 422
    assert (await api.post(path + "/events", headers=HEADERS, json=event)).status_code == 201
    assert (await api.post(path + "/events", headers=HEADERS, json=event)).status_code == 201
    audit = (await api.get(path + "/audit", headers=HEADERS)).json()
    assert [row["kind"] for row in audit["items"]].count("episode_ingested") == 1
    assert (await api.get(path + "/tasks?limit=101", headers=HEADERS)).status_code == 422


async def test_conversations_restore_sources_checkpoints_and_stable_pagination(
    autonomy_api, tmp_path,
):
    """数据库中的来源和步骤恢复为只读对话，按创建时间翻页且不混入另一身份。"""
    api = autonomy_api
    root = '/api/v1/autonomy/profiles'
    profile = (await api.post(root, headers=HEADERS, json=BODY)).json()
    path = root + '/' + profile['id']
    event = {'event_id': 'source', 'text': '请核对升级结果',
             'occurred_at': '2026-11-03T04:00:00Z'}
    await api.post(path + '/events', headers=HEADERS, json=event)
    task = (await api.post(path + '/tasks', headers=HEADERS, json={
        'request_id': 'conversation', 'goal': '核对后汇报',
        'steps': [{'action': 'report', 'text': '检查完成'}],
    })).json()
    # 用持久检查点模拟worker已提交，不在管理HTTP请求中执行或伪造模型调用。
    with sqlite3.connect(tmp_path / 'autonomy.db') as connection:
        document = json.loads(connection.execute(
            'SELECT document_json FROM agent_bot_autonomy_task WHERE id=?', (task['id'],)
        ).fetchone()[0])
        document.update(cursor=1, outputs=[{'text': '迁移核对完成', 'queued': False}])
        connection.execute("UPDATE agent_bot_autonomy_task SET status='succeeded',document_json=? "
                           'WHERE id=?', (json.dumps(document), task['id']))
    first = (await api.get(path + '/conversations?limit=1', headers=HEADERS)).json()
    assert first['items'][0]['task']['outputs'] == [{'text': '迁移核对完成', 'queued': False}]
    assert first['items'][0]['task']['status'] == 'succeeded'
    assert 'lease_token' not in first['items'][0]['task']
    assert first['next_before']
    second = (await api.get(path + '/conversations', headers=HEADERS,
                            params={'before': first['next_before'], 'limit': 1})).json()
    assert second['items'][0]['episode']['text'] == event['text']
    assert second['next_before'] is None
    other = (await api.post(root, headers=HEADERS, json={
        **BODY, 'owner': {**BODY['owner'], 'sender_id': '10003'},
    })).json()
    foreign = await api.get(root + '/' + other['id'] + '/conversations', headers=HEADERS,
                            params={'before': first['next_before']})
    assert foreign.json() == {'items': [], 'next_before': None}
    assert (await api.get(path + '/conversations?limit=51', headers=HEADERS)).status_code == 422


async def test_conversations_never_expose_expired_episode_text(autonomy_api, tmp_path):
    """即使后台未整理墓碑，对话读取也隐藏过期原文和摘要。"""
    api = autonomy_api
    profile = (await api.post('/api/v1/autonomy/profiles', headers=HEADERS, json=BODY)).json()
    path = '/api/v1/autonomy/profiles/' + profile['id']
    await api.post(path + '/events', headers=HEADERS, json={
        'event_id': 'expired', 'text': '到期后不可暴露的原文',
        'occurred_at': '2026-11-03T04:00:00Z',
    })
    with sqlite3.connect(tmp_path / 'autonomy.db') as connection:
        connection.execute("UPDATE agent_bot_autonomy_episode SET expires_at='2000-01-01'")
    response = await api.get(path + '/conversations', headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['items'][0]['source_expired'] is True
    assert response.json()['items'][0]['episode'] is None
    assert '到期后不可暴露的原文' not in response.text
