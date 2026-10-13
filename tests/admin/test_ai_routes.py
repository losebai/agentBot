import httpx
import pytest
from fastapi import FastAPI

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.contracts import ModelCreate, ProviderCreate
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.providers.ollama import OllamaProvider
from agent_bot.ai.providers.transport import ProviderTransport
from agent_bot.ai.service import AiService
from agent_bot.ai.sessions import AiSessionStore
from agent_bot.settings import DatabaseParams
from ai.fake_database import FakeDatabase
from ai.test_providers import ByteStream, stream_body

TOKEN = "offline-admin-token-longer-than-thirty-two-characters"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


async def test_provider_database_secret_is_write_only(python_api):
    """创建、编辑及列表响应不能回显供应商密钥。"""
    api, _, _ = python_api
    created = await api.post(
        "/api/v1/ai/providers",
        headers=HEADERS,
        json={
            "name": "second",
            "protocol": "openai-chat",
            "base_url": "https://second.invalid/v1",
            "credential": "independent-database-key",
            "enabled": True,
        },
    )
    assert created.status_code == 201
    assert created.json()["credential_set"] is True
    assert "credential" not in created.json()
    assert "independent-database-key" not in created.text
    listed = await api.get("/api/v1/ai/providers", headers=HEADERS)
    assert "independent-database-key" not in listed.text


async def test_upstream_models_use_provider_url_key_and_auth(python_api):
    """模型发现通过各供应商自己的地址鉴权，不允许匿名管理调用。"""
    api, _, runtime = python_api
    transport = runtime.ai_service.providers["ollama"].transport
    await transport.http.aclose()
    seen = []

    def handle(request):
        """只模拟列表 GET，不产生模型调用。"""
        seen.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(
            200, json={"models": [{"name": "exact/model"}, {"name": "exact/model"}]}
        )

    transport.http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    saved = await api.patch(
        "/api/v1/ai/providers/1",
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "credential": "provider-one-key",
        },
    )
    assert saved.status_code == 200
    path = "/api/v1/ai/providers/1/upstream-models?expected_revision=2"
    assert (await api.get(path)).status_code == 401
    response = await api.get(path, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["models"] == ["exact/model"]
    assert seen == [("https://offline.invalid/api/tags", "Bearer provider-one-key")]
    assert (await api.get(path.replace("=2", "=1"), headers=HEADERS)).status_code == 409


async def test_model_import_conflict_is_atomic_and_duplicates_are_skipped(python_api):
    """同名上游可跨供应商登记，别名冲突使整批回滚。"""
    api, _, _ = python_api
    response = await api.post(
        "/api/v1/ai/providers/1/models/import",
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "models": [
                {"alias": "new-one", "upstream_model": "one"},
                {"alias": "local", "upstream_model": "two"},
            ],
        },
    )
    assert response.status_code == 409
    assert len((await api.get("/api/v1/ai/models", headers=HEADERS)).json()["items"]) == 1
    response = await api.post(
        "/api/v1/ai/providers/1/models/import",
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "models": [
                {"alias": "already", "upstream_model": "exact/model"},
                {"alias": "new-one", "upstream_model": "one"},
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["skipped"] == ["exact/model"]
    assert response.json()["imported"][0]["enabled"] is False


@pytest.fixture
async def python_api(admin_store, admin_runtime):
    database = FakeDatabase()
    params = DatabaseParams("localhost", 3306, "offline", "offline", "offline")
    catalog = AiCatalog(params, connect=database.connect)
    provider = await catalog.create_provider(
        ProviderCreate(
            name="local", protocol="ollama", base_url="https://offline.invalid", enabled=True
        )
    )
    await catalog.create_model(
        ModelCreate(
            alias="local", upstream_model="exact/model", provider_id=provider.id, enabled=True
        )
    )
    service = AiService(
        catalog,
        AiSessionStore(params, connect=database.connect),
        CredentialResolver(),
        {
            "ollama": OllamaProvider(
                ProviderTransport(
                    httpx.MockTransport(
                        lambda req: (
                            httpx.Response(200, stream=ByteStream(stream_body("ollama")))
                            if __import__("json").loads(req.content)["stream"]
                            else httpx.Response(
                                200,
                                json={
                                    "message": {
                                        "role": "assistant", "content": "真实 mock 协议回复",
                                    },
                                    "done": True,
                                },
                            )
                        )
                    )
                )
            )
        },
    )
    admin_runtime.ai_service = service
    admin_runtime.config = admin_runtime.config.model_copy(update={"chat_model": "local"})
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=TOKEN))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    ) as client:
        yield client, app, admin_runtime
    await service.aclose()
    database.close()


async def test_ai_crud_auth_cas_and_safe_errors(python_api):
    api, _, _ = python_api
    assert (await api.get("/api/v1/ai/models")).status_code == 401
    models = await api.get("/api/v1/ai/models", headers=HEADERS)
    assert models.status_code == 200 and models.json()["items"][0]["alias"] == "local"
    payload = {"expected_revision": 1, "enabled": False}
    assert (
        await api.patch("/api/v1/ai/providers/1", headers=HEADERS, json=payload)
    ).status_code == 200
    conflict = await api.patch("/api/v1/ai/providers/1", headers=HEADERS, json=payload)
    assert conflict.status_code == 409 and conflict.json()["code"] == "AI_CONFIG_CONFLICT"


@pytest.mark.parametrize("system_prompt", ["测试提示", ""])
async def test_sync_stream_and_record_queries(python_api, system_prompt: str):
    """管理对话的同步、流式和记录功能兼容空机器人提示词。"""
    api, _, runtime = python_api
    runtime.config = runtime.config.model_copy(update={"chat_system_prompt": system_prompt})
    body = {"prompt": "hello", "session_id": "test", "messages": [], "model": "local"}
    response = await api.post("/api/v1/chat", headers=HEADERS, json=body)
    assert response.status_code == 200
    assert response.json()["content"] == "真实 mock 协议回复"
    assert response.json()["metadata"]["record_saved"] is True
    response = await api.post("/api/v1/chat/stream", headers=HEADERS, json=body)
    assert "event: delta" in response.text and "event: done" in response.text
    assert "event: error" not in response.text
    records = await api.get("/api/v1/ai/sessions", headers=HEADERS)
    assert len(records.json()["items"]) == 2 and "prompt" not in records.json()["items"][0]
    # 统一入口使用同一认证，列表不泄漏正文，详情保留输入和输出语义。
    assert (await api.get('/api/v1/ai/records')).status_code == 401
    combined = await api.get('/api/v1/ai/records?type=ordinary', headers=HEADERS)
    assert combined.status_code == 200
    items = combined.json()['items']
    assert len(items) == 2 and 'content' not in items[0]
    detail = await api.get(f'/api/v1/ai/records/{items[0]["id"]}', headers=HEADERS)
    assert detail.json()['content'] == 'hello'
    assert detail.json()['request_context']['messages'][-1]['content'] == 'hello'
    assert (await api.get('/api/v1/ai/records?type=bad', headers=HEADERS)).status_code == 422


async def test_system_records_api_filters_and_validates_category(python_api):
    """分组参数传入真实记录查询，普通页排除决策和指令，详情保留调用类型。"""
    from agent_bot.ai.call_records import CallRecordStore, legacy_record

    api, _, runtime = python_api
    sessions = runtime.ai_service.sessions
    store = CallRecordStore(sessions.params, connect=sessions.connect)
    await store.import_batch([
        legacy_record({"id": 1, "data": {"choices": []}}, "offline"),
        legacy_record({"id": 2, "data": {"type": "auto_speak_decision"}}, "offline"),
        legacy_record({"id": 3, "request_id": "qq-summary-test"}, "offline"),
    ])
    normal = await api.get("/api/v1/ai/records?category=conversation", headers=HEADERS)
    assert [r["source_id"] for r in normal.json()["items"]] == ["1"]
    system = await api.get("/api/v1/ai/records?category=system", headers=HEADERS)
    assert {r["system_kind"] for r in system.json()["items"]} == {"auto_speak", "command"}
    # 调用记录接口默认只查普通分组，客户端省略 category 也不会混入系统调用。
    default = await api.get("/api/v1/ai/records", headers=HEADERS)
    assert [r["source_id"] for r in default.json()["items"]] == ["1"]
    commands = await api.get(
        "/api/v1/ai/records?category=system&system_kind=command", headers=HEADERS,
    )
    assert [r["source_id"] for r in commands.json()["items"]] == ["3"]
    assert (await api.get("/api/v1/ai/records?category=bad", headers=HEADERS)).status_code == 422
    assert (await api.get("/api/v1/ai/records?category=system")).status_code == 401


async def test_status_statistics_use_live_counts_and_authenticated_archive(python_api):
    """真实认证接口汇总归档，历史 running 状态与模型发现不增加实时对话数。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent_bot.ai.call_records import CallRecordStore, legacy_record

    api, _, runtime = python_api
    service = runtime.ai_service
    store = CallRecordStore(service.sessions.params, connect=service.sessions.connect)
    await store.import_batch([legacy_record({
        "id": 1, "data": {"choices": [], "status": "running", "usage": {
            "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
            "cached_tokens": 30,
        }},
    }, "offline")])
    runtime.agent_service = SimpleNamespace(
        active_requests=2, readiness=AsyncMock(return_value=None),
    )
    assert (await api.get("/api/v1/status")).status_code == 401
    async with service._slot(), service._slot(conversation=False):
        response = await api.get("/api/v1/status", headers=HEADERS)
        assert response.status_code == 200, response.text
        stats = response.json()["statistics"]
        assert stats["conversation_count"] == 1
        assert stats["running_requests"] == 3
        assert stats["total_tokens"] == 110
        assert stats["cache_hit_rate"] == pytest.approx(0.3)
    runtime.agent_service.active_requests = 0
    response = await api.get("/api/v1/status", headers=HEADERS)
    assert response.json()["statistics"]["running_requests"] == 0


async def test_statistics_database_error_is_reported_without_secrets(python_api, monkeypatch):
    """统计失败返回明确错误，不能显示虚假零值或泄漏底层数据库详情。"""
    import pymysql

    api, _, runtime = python_api

    def broken(**kwargs):
        """只破坏记录连接，目录就绪与测试数据库保持独立。"""
        raise pymysql.OperationalError(2003, "private-database-secret")

    monkeypatch.setattr(runtime.ai_service.sessions, "connect", broken)
    response = await api.get("/api/v1/status", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["statistics"] is None
    assert response.json()["statistics_error"] == "AI_DATABASE_UNAVAILABLE"
    assert "private-database-secret" not in response.text


async def test_status_keeps_readiness_when_archive_schema_needs_upgrade(python_api):
    """未升级归档表的已有部署仍可查看服务状态，统计必须明确提示不可用。"""
    api, _, runtime = python_api
    sessions = runtime.ai_service.sessions
    await sessions.run(lambda c: c.db.connection.execute("DROP TABLE agent_bot_call_record"),
                       write=True)
    response = await api.get("/api/v1/status", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["statistics"] is None
    assert response.json()["statistics_error"] == "AI_NOT_INITIALIZED"
