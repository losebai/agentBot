"""知识只增只读，记忆修订与遗忘遵循管理员认证和版本边界。"""

import sqlite3

import httpx
import pytest
from fastapi import FastAPI

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from agent_bot.database import database_connector, initialize_database
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.knowledge.store import KnowledgeStore
from agent_bot.settings import DatabaseParams

TOKEN = "offline-knowledge-admin-password"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
async def knowledge_api(tmp_path, admin_store, admin_runtime):
    """管理接口使用隔离真实数据库，QQ 连接仍是现有运行时替身。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "admin.db"))
    initialize_database(params)
    admin_runtime.knowledge_store = KnowledgeStore(params, connect=database_connector(params))
    admin_runtime.tagged_memory_store = TaggedMemoryStore(params, secret="offline-memory")
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=TOKEN))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    ) as api:
        yield api, params


async def test_knowledge_and_memory_routes_require_admin_auth(knowledge_api):
    """所有目录和详情都安装到同一认证路由，不因新增页面而开放原文。"""
    api, _ = knowledge_api
    for path in (
        "knowledge", "knowledge/count", "knowledge-tags",
        "memories", "memory-tags", "memory-scopes",
    ):
        assert (await api.get("/api/v1/" + path)).status_code == 401
        assert (await api.get("/api/v1/" + path, headers=HEADERS)).status_code == 200


async def test_knowledge_direct_page_and_independent_filtered_count(knowledge_api):
    """统计路由先于整数详情路由匹配，筛选规范化与旧游标保持一致。"""
    api, _ = knowledge_api
    for title in ("部署一", "部署二", "部署三"):
        response = await api.post("/api/v1/knowledge", headers=HEADERS, json={
            "title": title, "content": "正文", "tags": ["部署"],
        })
        assert response.status_code == 201
    response = await api.get("/api/v1/knowledge?limit=1&page=2&tag=部署", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["items"][0]["title"] == "部署二"
    response = await api.get(
        "/api/v1/knowledge/count?query=%20部署%20&tag=%20部署%20", headers=HEADERS,
    )
    assert response.status_code == 200 and response.json() == {"total": 3}
    cursor = (await api.get("/api/v1/knowledge?limit=1", headers=HEADERS)).json()["next_cursor"]
    for params in ({"page": 0}, {"page": 1, "cursor": cursor}):
        response = await api.get("/api/v1/knowledge", headers=HEADERS, params=params)
        assert response.status_code == 422


async def test_knowledge_stays_read_only_and_memory_patch_requires_revision(knowledge_api):
    """知识禁止编辑，记忆不能通过无版本的 PATCH 或任意 PUT 覆盖当前事实。"""
    api, _ = knowledge_api
    for path in ("knowledge/1", "memories/1"):
        for method in ("PATCH", "PUT"):
            expected = 422 if path == "memories/1" and method == "PATCH" else 405
            assert (
                await api.request(
                    method, "/api/v1/" + path, headers=HEADERS, json={"content": "覆盖"}
                )
            ).status_code == expected


async def test_admin_create_delete_and_conflicts(knowledge_api):
    """新增、同键拒绝和删除结果对用户明确可见，不静默恢复记忆。"""
    api, _ = knowledge_api
    item = await api.post(
        "/api/v1/knowledge",
        headers=HEADERS,
        json={"title": "说明", "content": "中文内容", "tags": ["部署"]},
    )
    assert item.status_code == 201
    id = item.json()["id"]
    assert (await api.delete(f"/api/v1/knowledge/{id}", headers=HEADERS)).status_code == 204
    assert (await api.get(f"/api/v1/knowledge/{id}", headers=HEADERS)).status_code == 404
    body = {
        "scope": "user",
        "bot_id": "10001",
        "target": "10002",
        "key": "偏好",
        "content": "原文",
        "tags": ["语言"],
    }
    first = await api.post("/api/v1/memories", headers=HEADERS, json=body)
    assert first.status_code == 201
    duplicate = await api.post(
        "/api/v1/memories", headers=HEADERS, json={**body, "content": "新文"}
    )
    assert duplicate.status_code == 409
    id = first.json()["id"]
    assert (await api.get(f"/api/v1/memories/{id}", headers=HEADERS)).json()["content"] == "原文"
    deleted = await api.delete(
        f"/api/v1/memories/{id}?expected_revision={first.json()['revision']}", headers=HEADERS,
    )
    assert deleted.status_code == 204
    assert (await api.post("/api/v1/memories", headers=HEADERS, json=body)).status_code == 409


async def test_missing_knowledge_schema_does_not_break_status(knowledge_api):
    """缺表只拒绝知识请求，基础状态和原有记忆管理保持可用。"""
    api, params = knowledge_api
    with sqlite3.connect(params.sqlite_path) as db:
        db.execute("DROP TABLE chat_knowledge_tag")
        db.execute("DROP TABLE chat_knowledge")
    response = await api.get("/api/v1/knowledge", headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "KNOWLEDGE_NOT_INITIALIZED"
    assert (await api.get("/api/v1/status", headers=HEADERS)).status_code == 200
    assert (await api.get("/api/v1/memories", headers=HEADERS)).status_code == 200


async def test_memory_scope_titles_use_known_directory_and_update_without_rewriting(knowledge_api):
    """管理员标题来自已知目录；昵称变化只改变显示，不改记忆和匿名授权标识。"""
    api, params = knowledge_api
    with sqlite3.connect(params.sqlite_path) as db:
        db.execute(
            "INSERT INTO chat_group_member (bot_id,group_id,user_id,group_name,nickname,card) "
            "VALUES (?,?,?,?,?,?)", ("10001", "20001", "30001", "读书群", "小明", "群名片"),
        )
    response = await api.post("/api/v1/memories", headers=HEADERS, json={
        "scope": "user", "bot_id": "10001", "target": "30001",
        "key": "语言", "content": "喜欢中文", "tags": ["语言"],
    })
    assert response.status_code == 201
    item = response.json()
    assert item["scope_title"] == "小明 (30001) · 机器人 10001"
    scopes = (await api.get("/api/v1/memory-scopes", headers=HEADERS)).json()
    assert scopes[0]["scope_title"] == item["scope_title"]
    assert (await api.get("/api/v1/memories", headers=HEADERS)).json()["items"][0][
        "scope_title"
    ] == item["scope_title"]
    with sqlite3.connect(params.sqlite_path) as db:
        db.execute("UPDATE chat_group_member SET nickname=?", ("新昵称",))
    detail = (await api.get(f"/api/v1/memories/{item['id']}", headers=HEADERS)).json()
    assert detail["scope_title"] == "新昵称 (30001) · 机器人 10001"
    assert detail["scope_id"] == item["scope_id"] and detail["content"] == item["content"]
    unauthorized = await api.get("/api/v1/memory-scopes")
    assert unauthorized.status_code == 401 and "新昵称" not in unauthorized.text
