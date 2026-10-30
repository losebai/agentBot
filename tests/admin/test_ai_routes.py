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
    assert response.json()["code"] == "AI_MODEL_ALIAS_CONFLICT"
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


async def test_model_sync_reports_alias_conflicts_and_imports_other_models(python_api):
    """自动同步保留同名记录，明确返回冲突并允许用新别名登记。"""
    api, _, _ = python_api
    created = await api.post(
        "/api/v1/ai/providers",
        headers=HEADERS,
        json={"name": "second", "protocol": "ollama", "base_url": "http://second.invalid"},
    )
    provider_id = created.json()["id"]
    path = f"/api/v1/ai/providers/{provider_id}/models/import"
    response = await api.post(
        path,
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "skip_alias_conflicts": True,
            "models": [
                {"alias": "local", "upstream_model": "exact/model"},
                {"alias": "other", "upstream_model": "other"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["conflicts"] == [{"alias": "local", "upstream_model": "exact/model"}]
    assert [item["alias"] for item in body["imported"]] == ["other"]
    assert body["skipped"] == []
    models = (await api.get("/api/v1/ai/models", headers=HEADERS)).json()["items"]
    original = next(item for item in models if item["alias"] == "local")
    assert original["provider_id"] == 1 and original["enabled"] is True
    resolved = await api.post(
        path,
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "models": [{"alias": "second-local", "upstream_model": "exact/model"}],
        },
    )
    assert resolved.status_code == 200 and resolved.json()["conflicts"] == []
    assert resolved.json()["imported"][0]["alias"] == "second-local"
    assert resolved.json()["imported"][0]["enabled"] is False
    # 重新同步跳过已登记的上游模型，不覆盖管理员手动设置的别名。
    repeated = await api.post(
        path,
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "skip_alias_conflicts": True,
            "models": [{"alias": "local", "upstream_model": "exact/model"}],
        },
    )
    assert repeated.json()["skipped"] == ["exact/model"]
    assert repeated.json()["conflicts"] == []


async def test_model_sync_conflict_reporting_preserves_provider_revision_check(python_api):
    """允许报告别名冲突不能绕过供应商版本检查或导入旧配置的模型。"""
    api, _, _ = python_api
    saved = await api.patch(
        "/api/v1/ai/providers/1",
        headers=HEADERS,
        json={"expected_revision": 1, "enabled": False},
    )
    assert saved.status_code == 200
    response = await api.post(
        "/api/v1/ai/providers/1/models/import",
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "skip_alias_conflicts": True,
            "models": [{"alias": "new", "upstream_model": "new"}],
        },
    )
    assert response.status_code == 409 and response.json()["code"] == "AI_CONFIG_CONFLICT"
    assert len((await api.get("/api/v1/ai/models", headers=HEADERS)).json()["items"]) == 1


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


async def test_status_memory_statistics_uses_utc_dates_and_independent_errors(python_api):
    """记忆按 UTC 过滤，失败不遮蔽用量及就绪信息，认证仍先于数据库读取。"""
    from datetime import datetime
    from unittest.mock import Mock

    from agent_bot.integrations.tagged_memory_store import TaggedMemoryStoreError
    from agent_bot.memory.contracts import MemoryStatistics

    api, _, runtime = python_api
    runtime.tagged_memory_store = Mock()
    reader = runtime.tagged_memory_store.admin_statistics
    reader.return_value = MemoryStatistics(
        memory_count=12, recall_count=4, recall_hit_count=3, recall_rate=0.75,
    )
    path = "/api/v1/status?start_date=2026-10-30&end_date=2026-10-30"
    assert (await api.get(path)).status_code == 401
    reader.assert_not_called()
    response = await api.get(path, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["memory_statistics"]["recall_rate"] == 0.75
    reader.assert_called_once_with(
        start_time=datetime(2026, 10, 29, 16), end_time=datetime(2026, 10, 30, 16),
    )
    reader.side_effect = TaggedMemoryStoreError("private database detail")
    response = await api.get(path, headers=HEADERS)
    payload = response.json()
    assert response.status_code == 200
    assert payload["memory_statistics"] is None
    assert payload["memory_statistics_error"] == "MEMORY_UNAVAILABLE"
    assert payload["statistics"] is not None
    assert payload["ready"] is True
    assert "private database detail" not in response.text


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


async def test_numbered_record_api_and_independent_count_keep_authentication(python_api):
    """页码查询和独立统计沿用管理认证，相同筛选得到一致总数，不返回归档正文。"""
    from agent_bot.ai.call_records import CallRecordStore, legacy_record

    api, _, runtime = python_api
    sessions = runtime.ai_service.sessions
    store = CallRecordStore(sessions.params, connect=sessions.connect)
    await store.import_batch([
        legacy_record({"id": i, "session_Id": "paged", "data": {"choices": []}}, "offline")
        for i in range(1, 4)
    ])
    assert (await api.get("/api/v1/ai/records/count?session_id=paged")).status_code == 401
    count = await api.get("/api/v1/ai/records/count?session_id=paged", headers=HEADERS)
    assert count.status_code == 200 and count.json() == {"total": 3}
    page = await api.get("/api/v1/ai/records?session_id=paged&page=2&limit=2", headers=HEADERS)
    assert page.status_code == 200
    assert [row["source_id"] for row in page.json()["items"]] == ["1"]
    assert "content" not in page.json()["items"][0]
    for query in ("page=0", "page=1&cursor=bad"):
        assert (await api.get(f"/api/v1/ai/records?{query}", headers=HEADERS)).status_code == 422
    invalid = await api.get("/api/v1/ai/records/count?category=bad", headers=HEADERS)
    assert invalid.status_code == 422


async def test_management_refresh_returns_public_error_without_waiting(python_api, monkeypatch):
    """运行时刷新时记录请求返回明确 503，不暴露内部异常或持续等待模型完成。"""
    from contextlib import asynccontextmanager

    from agent_bot.core.lifecycle import RuntimeRefreshInProgressError

    api, _, runtime = python_api

    @asynccontextmanager
    async def refreshing(*, read_only: bool = False):
        """模拟已关闭新租约入口的刷新状态，不修改数据库或运行快照。"""
        raise RuntimeRefreshInProgressError()
        yield

    monkeypatch.setattr(runtime, "lease", refreshing)
    for path in ("ai/records?limit=20&page=1", "ai/records/count", "status"):
        response = await api.get("/api/v1/" + path, headers=HEADERS)
        assert response.status_code == 503
        assert response.json()["code"] == "RUNTIME_REFRESHING"
        assert "配置正在更新" in response.json()["message"]
        assert response.json()["request_id"]
    response = await api.post(
        "/api/v1/chat/stream", headers=HEADERS,
        json={"prompt": "离线测试", "session_id": "refresh", "messages": []},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "RUNTIME_REFRESHING"


async def test_management_reads_remain_available_during_model_drain(python_api, monkeypatch):
    """真实门闩排空长推理时仍可读统计和目录；认证及新推理的刷新限制不变。"""
    from types import SimpleNamespace

    from agent_bot.admin.application_runtime import ApplicationRuntimeProvider
    from agent_bot.core.lifecycle import RuntimeLeaseGate

    api, _, runtime = python_api
    gate = RuntimeLeaseGate()
    provider = ApplicationRuntimeProvider(SimpleNamespace(lease=gate.lease), None)
    monkeypatch.setattr(runtime, "lease", provider.lease)
    async with gate.lease():
        await gate.begin_refresh(allow_readers=True)
        try:
            for path in ("status", "ai/records", "ai/records/count", "ai/models", "ai/providers"):
                response = await api.get("/api/v1/" + path, headers=HEADERS)
                assert response.status_code == 200, response.text
                assert (await api.get("/api/v1/" + path)).status_code == 401
            response = await api.post(
                "/api/v1/chat/stream", headers=HEADERS,
                json={"prompt": "离线测试", "session_id": "refresh", "messages": []},
            )
            assert response.status_code == 503
            assert response.json()["code"] == "RUNTIME_REFRESHING"
        finally:
            await gate.end_refresh()


async def test_status_statistics_use_live_counts_and_authenticated_archive(python_api):
    """真实认证接口汇总归档，历史 running 状态与模型发现不增加实时对话数。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent_bot.ai.call_records import CallRecordStore, legacy_record

    api, _, runtime = python_api
    service = runtime.ai_service
    store = CallRecordStore(service.sessions.params, connect=service.sessions.connect)
    await store.import_batch([legacy_record({
        "id": 1, "create_time": "2026-10-28 12:00:00",
        "data": {"choices": [], "status": "running", "usage": {
            "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
            "cached_tokens": 30,
        }},
    }, "offline")])
    runtime.agent_service = SimpleNamespace(
        active_requests=2, readiness=AsyncMock(return_value=None),
    )
    path = "/api/v1/status?start_date=2026-10-28&end_date=2026-10-28"
    assert (await api.get(path)).status_code == 401
    async with service._slot(), service._slot(conversation=False):
        response = await api.get(path, headers=HEADERS)
        assert response.status_code == 200, response.text
        stats = response.json()["statistics"]
        assert stats["conversation_count"] == 1
        assert stats["running_requests"] == 3
        assert stats["total_tokens"] == 110
        assert stats["cache_hit_rate"] == pytest.approx(0.3)
    runtime.agent_service.active_requests = 0
    response = await api.get("/api/v1/status", headers=HEADERS)
    assert response.json()["statistics"]["running_requests"] == 0


async def test_status_defaults_to_beijing_today_and_supports_date_ranges(python_api):
    """日期按归档北京时间包含整天；默认今日，非法或不完整区间明确拒绝。"""
    from datetime import datetime, timedelta, timezone

    from agent_bot.ai.call_records import CallRecordStore, legacy_record

    api, _, runtime = python_api
    sessions = runtime.ai_service.sessions
    store = CallRecordStore(sessions.params, connect=sessions.connect)
    today = datetime.now(timezone(timedelta(hours=8))).date()
    yesterday = today - timedelta(days=1)
    await store.import_batch([
        legacy_record({"id": 1, "create_time": f"{today} 00:00:00",
                       "data": {"choices": [], "status": "failed"}}, "status-range"),
        legacy_record({"id": 2, "create_time": f"{yesterday} 23:59:59",
                       "data": {"choices": [], "status": "completed"}}, "status-range"),
    ])
    response = await api.get("/api/v1/status", headers=HEADERS)
    assert response.status_code == 200
    stats = response.json()["statistics"]
    assert stats["conversation_count"] == 1
    assert stats["model_failure_rate"] == 1
    response = await api.get(
        f"/api/v1/status?start_date={yesterday}&end_date={today}", headers=HEADERS,
    )
    assert response.json()["statistics"]["model_failure_rate"] == 0.5
    for query in (
        f"start_date={today}&end_date={yesterday}", f"start_date={today}",
        f"end_date={today}", "start_date=invalid&end_date=2026-10-28",
        "start_date=0001-01-01&end_date=0001-01-02",
    ):
        response = await api.get(f"/api/v1/status?{query}", headers=HEADERS)
        assert response.status_code == 422


async def test_call_record_usage_is_available_in_list_and_detail(python_api):
    """认证接口提供历史协议用量；列表只返回数值摘要，不暴露完整响应和请求正文。"""
    from agent_bot.ai.call_records import CallRecordStore, legacy_record

    api, _, runtime = python_api
    sessions = runtime.ai_service.sessions
    store = CallRecordStore(sessions.params, connect=sessions.connect)
    await store.import_batch([legacy_record({
        "id": 1, "content": "只在详情显示的正文", "data": {"choices": [], "usage": {
            "prompt_tokens": 100, "completion_tokens": 10,
            "prompt_tokens_details": {"cached_tokens": 0},
        }},
    }, "usage-api")])
    response = await api.get("/api/v1/ai/records", headers=HEADERS)
    assert response.status_code == 200
    summary = response.json()["items"][0]
    assert summary["usage"] == {
        "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
        "cached_tokens": 0, "cache_prompt_tokens": 100,
    }
    assert "data" not in summary and "content" not in summary
    detail = await api.get(f"/api/v1/ai/records/{summary['id']}", headers=HEADERS)
    assert detail.status_code == 200
    assert detail.json()["usage"] == summary["usage"]


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


async def test_status_keeps_queue_readiness_when_archive_schema_needs_upgrade(python_api):
    """队列就绪不受归档影响，普通目录和调用统计各自报告缺失结构。"""
    api, _, runtime = python_api
    sessions = runtime.ai_service.sessions
    await sessions.run(lambda c: c.db.connection.execute("DROP TABLE agent_bot_call_record"),
                       write=True)
    response = await api.get("/api/v1/status", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["statistics"] is None
    assert response.json()["statistics_error"] == "AI_NOT_INITIALIZED"
