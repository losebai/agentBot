"""记忆管理生命周期接口使用隔离 SQLite 验证认证、版本和预览批次。"""

import httpx
import pytest
from fastapi import FastAPI

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from agent_bot.database import database_connector, initialize_database
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.knowledge.store import KnowledgeStore
from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity
from agent_bot.settings import DatabaseParams

TOKEN = "offline-memory-lifecycle-admin-password"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.fixture
async def lifecycle_api(tmp_path, admin_store, admin_runtime):
    """管理运行时只绑定临时文件，不连接 QQ、真实数据库或模型。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "lifecycle.db"))
    initialize_database(params)
    admin_runtime.tagged_memory_store = TaggedMemoryStore(params, secret="offline-lifecycle")
    admin_runtime.knowledge_store = KnowledgeStore(params, connect=database_connector(params))
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=TOKEN))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    ) as client:
        yield client


async def create_memory(api, key="网关限制", content="413 来自网关请求体限制", **extra):
    """通过生产新增接口建立可追溯版本，拒绝直接修改测试库模拟成功。"""
    response = await api.post(
        "/api/v1/memories", headers=HEADERS,
        json={"scope": "user", "bot_id": "10001", "target": "10002", "key": key,
              "content": content, "tags": ["部署"], "topic": "413 网关报错", **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_lifecycle_routes_require_admin_and_keep_knowledge_read_only(lifecycle_api):
    """新增修改、历史和预览都复用管理认证，知识条目仍然禁止编辑。"""
    api = lifecycle_api
    for method, path, body in (
        ("PATCH", "memories/1", {"expected_revision": 1, "reason": "核对"}),
        ("GET", "memories/1/revisions", None),
        ("POST", "memories/merge-preview", {}),
        ("POST", "memories/merge", {}),
        ("POST", "memories/cleanup-preview", {}),
        ("POST", "memories/cleanup", {}),
    ):
        response = await api.request(method, "/api/v1/" + path, json=body)
        assert response.status_code == 401
    assert (await api.patch("/api/v1/knowledge/1", headers=HEADERS, json={})).status_code == 405
    assert (await api.put("/api/v1/memories/1", headers=HEADERS, json={})).status_code == 405


async def test_patch_is_sparse_and_rejects_stale_revision(lifecycle_api):
    """正文修订不能覆盖未编辑字段；陈旧版本不能产生额外历史。"""
    api = lifecycle_api
    item = await create_memory(api, kind="fact", valid_until="2030-01-01T00:00:00Z")
    path = f"/api/v1/memories/{item['id']}"
    update = {"expected_revision": item["revision"], "content": "413 已确认来自反向代理",
              "reason": "根据日志更正"}
    response = await api.patch(path, headers=HEADERS, json=update)
    assert response.status_code == 200
    revised = response.json()
    assert revised["revision"] == 2
    assert revised["content"] == update["content"]
    for name in ("tags", "topic", "kind", "valid_until", "scope_id"):
        assert revised[name] == item[name]
    conflict = await api.patch(path, headers=HEADERS, json={**update, "content": "陈旧覆盖"})
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "MEMORY_REVISION_CONFLICT"
    assert "版本" in conflict.json()["message"]
    history = await api.get(path + "/revisions", headers=HEADERS)
    assert history.status_code == 200
    versions = history.json()["items"]
    assert [version["revision"] for version in versions] == [2, 1]
    assert versions[0]["reason"] == update["reason"]
    assert versions[1]["snapshot"]["content"] == item["content"]
    assert (await api.patch(path, headers=HEADERS, json={"content": "缺少版本"})).status_code == 422


async def test_delete_requires_version_and_history_remains_admin_only(lifecycle_api):
    """遗忘中断普通详情和召回，但认证审计保留旧原文且不提供恢复。"""
    api = lifecycle_api
    item = await create_memory(api)
    path = f"/api/v1/memories/{item['id']}"
    assert (await api.delete(path, headers=HEADERS)).status_code == 422
    stale = await api.delete(path + "?expected_revision=99", headers=HEADERS)
    assert stale.status_code == 409
    assert (await api.get(path, headers=HEADERS)).json()["content"] == item["content"]
    assert (await api.delete(path + "?expected_revision=1", headers=HEADERS)).status_code == 204
    assert (await api.get(path, headers=HEADERS)).status_code == 404
    history = (await api.get(path + "/revisions", headers=HEADERS)).json()["items"]
    assert history[0]["operation"] == "forget"
    assert history[-1]["snapshot"]["content"] == item["content"]
    assert (await api.get(path + "/revisions")).status_code == 401


async def test_cleanup_commits_only_previewed_versions(lifecycle_api):
    """预览后出现的同主题事实不受清理影响，摘要错误不写入任何记录。"""
    api = lifecycle_api
    first = await create_memory(api)
    second = await create_memory(api, "代理限制", "另一个代理环境也有请求体限制")
    preview = await api.post("/api/v1/memories/cleanup-preview", headers=HEADERS, json={
        "scope_id": first["scope_id"], "topic": first["topic"], "action": "expire",
    })
    assert preview.status_code == 200
    batch = preview.json()
    assert {item["id"] for item in batch["items"]} == {first["id"], second["id"]}
    late = await create_memory(api, "新环境", "新环境的限制仍有效")
    body = {"items": [{"id": item["id"], "expected_revision": item["revision"]}
                      for item in batch["items"]], "digest": batch["digest"],
            "action": "expire", "reason": "旧环境已下线"}
    rejected = await api.post("/api/v1/memories/cleanup", headers=HEADERS,
                              json={**body, "digest": "0" * 64})
    assert rejected.status_code == 409
    assert (await api.get(f"/api/v1/memories/{first['id']}", headers=HEADERS)).json()[
        "status"
    ] == "active"
    cleaned = await api.post("/api/v1/memories/cleanup", headers=HEADERS, json=body)
    assert cleaned.status_code == 200 and cleaned.json()["affected"] == 2
    active = (await api.get("/api/v1/memories", headers=HEADERS)).json()["items"]
    assert [item["id"] for item in active] == [late["id"]]
    expired = (await api.get("/api/v1/memories?status=expired&kind=fact", headers=HEADERS)).json()
    assert {item["id"] for item in expired["items"]} == {first["id"], second["id"]}


async def test_merge_previews_versions_and_normalizes_input_errors(lifecycle_api):
    """合并先预览，重复目标等非法条件返回固定错误而非内部异常详情。"""
    api = lifecycle_api
    first = await create_memory(api)
    second = await create_memory(api, "重复结论", "相同环境的网关请求体限制")
    items = [{"id": item["id"], "expected_revision": item["revision"]}
             for item in (first, second)]
    body = {"target_id": first["id"], "items": items, "reason": "同一主体的重复结论"}
    assert (await api.post("/api/v1/memories/merge", headers=HEADERS, json=body)).status_code == 422
    duplicate = await api.post("/api/v1/memories/merge-preview", headers=HEADERS,
                               json={**body, "items": [items[0], items[0]]})
    assert duplicate.status_code == 422
    assert duplicate.json()["code"] == "MEMORY_INVALID_INPUT"
    assert "duplicate memory target" not in duplicate.text
    preview = await api.post("/api/v1/memories/merge-preview", headers=HEADERS, json=body)
    assert preview.status_code == 200
    unchanged = await api.get(f"/api/v1/memories/{second['id']}", headers=HEADERS)
    assert unchanged.json()["status"] == "active"
    merged = await api.post("/api/v1/memories/merge", headers=HEADERS,
                            json={**body, "digest": preview.json()["digest"]})
    assert merged.status_code == 200
    assert merged.json()["revision"] == 2
    sources = (await api.get("/api/v1/memories?status=merged", headers=HEADERS)).json()["items"]
    assert [item["id"] for item in sources] == [second["id"]]


async def test_lifecycle_filters_bind_pagination_and_reject_unknown_status(lifecycle_api):
    """主题、类型与状态参与分页身份，改变筛选不能沿用旧游标。"""
    api = lifecycle_api
    await create_memory(api)
    await create_memory(api, "网关判断", "网关故障的补充说明")
    first = await api.get("/api/v1/memories?limit=1&topic=413%20网关报错&kind=fact",
                          headers=HEADERS)
    assert first.status_code == 200
    cursor = first.json()["next_cursor"]
    assert cursor
    changed = await api.get("/api/v1/memories", headers=HEADERS,
                            params={"limit": 1, "cursor": cursor, "topic": "其他主题"})
    assert changed.status_code == 422
    invalid = await api.get("/api/v1/memories?status=unsupported", headers=HEADERS)
    assert invalid.status_code == 422


async def test_resolve_requires_whole_conflict_set_and_current_versions(
    lifecycle_api, admin_runtime,
):
    """完整冲突集合必须一次采用，漏项或陈旧版本不能留下部分仲裁结果。"""
    api = lifecycle_api
    original = await create_memory(api, scope="global", target=None)
    store = admin_runtime.tagged_memory_store
    alternatives = []
    for index, content in enumerate(("413 来自客户端限制", "413 来自模型服务限制")):
        identity = MemoryIdentity(bot_id="10001", user_id="10002", session_id="offline-conflict",
                                  source_text=content, source_event_id=f"conflict-event-{index}")
        result = store.execute(identity, MemoryArguments(
            action="revise", scope="global", id=original["id"], expected_revision=1,
            content=content, reason="报告不同结论", source_excerpt=content,
            retention_reason="correction",
        ))
        assert result.status == "pending"
        alternatives.append(result.records[0].id)
    path = f"/api/v1/memories/{alternatives[0]}/conflicts"
    assert (await api.get(path)).status_code == 401
    listing = await api.get(path, headers=HEADERS)
    assert listing.status_code == 200
    candidates = listing.json()["items"]
    assert {item["id"] for item in candidates} == {original["id"], *alternatives}
    body = {"target_id": alternatives[0], "reason": "根据完整日志采用此结论",
            "items": [{"id": item["id"], "expected_revision": item["revision"]}
                      for item in candidates]}
    assert (await api.post("/api/v1/memories/resolve", json=body)).status_code == 401
    incomplete = await api.post("/api/v1/memories/resolve", headers=HEADERS,
                                json={**body, "items": body["items"][:2]})
    assert incomplete.status_code == 409
    stale = {**body, "items": [{**item, "expected_revision": 99} for item in body["items"]]}
    rejected = await api.post("/api/v1/memories/resolve", headers=HEADERS, json=stale)
    assert rejected.status_code == 409
    assert store.admin_get(original["id"]).status == "active"
    accepted = await api.post("/api/v1/memories/resolve", headers=HEADERS, json=body)
    assert accepted.status_code == 200 and accepted.json()["id"] == alternatives[0]
    assert accepted.json()["status"] == "active"
    assert accepted.json()["conflict_set_id"] is None
    assert all(store.admin_get(id).status == "expired"
               for id in (original["id"], alternatives[1]))
