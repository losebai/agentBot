"""Exercise real ASGI management routes with isolated persistence and HTTP transports."""


import httpx
import pytest
from fastapi import FastAPI

from ai.conftest import ai_database as ai_database
from ai.conftest import ai_params as ai_params
from ai.conftest import catalog as catalog
from ai.test_service import make_service

TOKEN = "offline-admin-token-longer-than-thirty-two-characters"


async def test_static_onebot_token_patch_is_rejected_without_database_changes(
    api, admin_store, admin_runtime,
):
    """网页不可写启动 Token，拒绝后配置版本和运行认证均保持原样。"""
    token = "management-onebot-token-with-at-least-32-characters"
    original = admin_store.load_values()
    running = admin_runtime.config.onebot_v11_access_token
    response = await api.patch("/api/v1/settings", headers=HEADERS, json={
        "expected_revision": 1, "values": {"onebot_v11_access_token": token},
    })
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST" and token not in response.text
    assert admin_store.load_values() == original
    assert admin_runtime.config.onebot_v11_access_token == running


async def test_static_onebot_token_snapshot_is_readonly_and_redacted(
    api, admin_store, admin_runtime,
):
    """静态 Token 只公开已设置状态及重启要求，不回显启动凭据。"""
    response = await api.get("/api/v1/settings", headers=HEADERS)
    assert response.status_code == 200
    field = next(
        row for row in response.json()["fields"] if row["name"] == "onebot_v11_access_token"
    )
    assert field["scope"] == "static" and field["read_only"] and field["restart_required"]
    assert field["value"] is None and field["secret_is_set"]
    assert admin_runtime.config.onebot_v11_access_token.get_secret_value() not in response.text


HEADERS = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def admin_app(admin_store, admin_runtime):
    """Install routes on an actual FastAPI app without running plugin startup hooks."""
    from agent_bot.admin.config import AdminConfig
    from agent_bot.admin.routes import install_admin

    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=TOKEN))
    return app


@pytest.fixture
async def api(admin_app):
    """ASGITransport avoids a listening socket and external requests."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=admin_app), base_url="http://offline.test"
    ) as client:
        yield client


async def test_liveness_is_public_but_management_requires_credentials(api):
    assert (await api.get("/health/live")).status_code == 200
    response = await api.get("/api/v1/settings")
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("password", ["", "Easy!123"])
async def test_auth_mode_and_management_password_boundary(admin_store, admin_runtime, password):
    """公开接口只报告是否需要密码；读写管理接口遵循同一权限边界。"""
    from agent_bot.admin.config import AdminConfig
    from agent_bot.admin.routes import install_admin

    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=password))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline.test"
    ) as client:
        mode = await client.get("/api/v1/auth")
        assert mode.status_code == 200
        assert mode.json() == {"password_required": bool(password)}
        assert mode.headers["cache-control"] == "no-store"
        update = {"expected_revision": 1, "values": {"chat_model": "new-model"}}
        read = await client.get("/api/v1/settings")
        write = await client.patch("/api/v1/settings", json=update)
        assert read.status_code == (401 if password else 200)
        assert write.status_code == (401 if password else 200)
        if password:
            wrong = await client.get(
                "/api/v1/settings", headers={"Authorization": "Bearer Wrong!123"}
            )
            assert wrong.status_code == 401
            headers = {"Authorization": f"Bearer {password}"}
            assert (await client.get("/api/v1/settings", headers=headers)).status_code == 200
            saved = await client.patch("/api/v1/settings", json=update, headers=headers)
            assert saved.status_code == 200


async def test_status_without_connected_bots_is_ready(api):
    response = await api.get("/api/v1/status", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["bot_count"] == 0
    assert response.json()["ai_backend"] == "python-ordinary"
    assert (await api.get("/api/v1/bots", headers=HEADERS)).json() == []


async def test_settings_conflict_and_secret_validation_are_safe(api):
    """运行字段保留版本锁，静态字段通过真实 HTTP 请求被拒绝并脱敏。"""
    response = await api.patch(
        "/api/v1/settings",
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "values": {"chat_model": "new-model"},
        },
    )
    assert response.status_code == 200
    assert response.json()["restart_required_fields"] == []
    assert response.json()["database_revision"] == 2
    assert response.json()["applied_revision"] == 1
    conflict = await api.patch(
        "/api/v1/settings",
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "values": {"chat_model": "stale-model"},
        },
    )
    assert conflict.status_code == 409
    invalid = await api.patch(
        "/api/v1/settings",
        headers=HEADERS,
        json={
            "expected_revision": 2,
            "values": {"onebot_v11_access_token": "private-secret"},
        },
    )
    assert invalid.status_code == 422
    assert "private-secret" not in invalid.text
    assert invalid.json()["request_id"] == invalid.headers["x-request-id"]


async def test_database_failure_has_safe_distinct_error(api, admin_store, monkeypatch):
    from agent_bot.settings import SettingsNotInitializedError

    def fail():
        raise SettingsNotInitializedError("private driver error")

    monkeypatch.setattr(admin_store, "load", fail)
    response = await api.get("/api/v1/settings", headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "SETTINGS_NOT_INITIALIZED"
    assert "private driver error" not in response.text


async def test_oversized_chunked_body_is_rejected(api):
    async def chunks():
        yield b'{"prompt":"'
        yield b"x" * 70000
        yield b'"}'

    response = await api.post("/api/v1/chat", headers=HEADERS, content=chunks())
    assert response.status_code == 413


async def test_chat_only_calls_plain_ai_and_returns_real_reply(
    api, admin_runtime, catalog, ai_params, ai_database,
):
    """管理入口仅调用本地普通服务，系统提示和模型目标均由服务端决定。"""
    service = await make_service(catalog, ai_params, ai_database, lambda req: httpx.Response(
        200, json={"message": {"role": "assistant", "content": "actual reply"}, "done": True},
    ))
    admin_runtime.ai_service = service
    admin_runtime.config = admin_runtime.config.model_copy(update={"chat_model": "local"})
    try:
        response = await api.post(
            "/api/v1/chat", headers=HEADERS, json={"prompt": "hello", "session_id": "web-test"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["content"] == "actual reply"
        assert response.json()["metadata"]["model"] == "exact/model"
    finally:
        await service.aclose()


async def test_ai_timeout_is_safe_and_not_a_success(
    api, admin_runtime, catalog, ai_params, ai_database,
):
    """上游超时经本地错误分类返回，管理响应不泄漏模型端错误。"""
    def timeout(request):
        """只模拟传输超时，不连接实际服务。"""
        raise httpx.ReadTimeout("private-upstream-token", request=request)

    service = await make_service(catalog, ai_params, ai_database, timeout)
    admin_runtime.ai_service = service
    admin_runtime.config = admin_runtime.config.model_copy(update={"chat_model": "local"})
    try:
        response = await api.post(
            "/api/v1/chat", headers=HEADERS, json={"prompt": "hello", "session_id": "web-test"}
        )
        assert response.status_code == 504
        assert "private-upstream-token" not in response.text
    finally:
        await service.aclose()


async def test_chat_refuses_browser_selected_tools_or_target(api):
    for field, value in (("tools", ["job"]), ("base_url", "http://private.test")):
        response = await api.post(
            "/api/v1/chat",
            headers=HEADERS,
            json={
                "prompt": "hello",
                "session_id": "web-test",
                field: value,
            },
        )
        assert response.status_code == 422
        assert str(value) not in response.text


async def test_unknown_management_failure_is_typed_without_internal_details(
    api, admin_runtime, monkeypatch
):
    """Unexpected failures must not bypass the safe management error boundary."""

    async def failure():
        raise RuntimeError("internal-private-credential")

    monkeypatch.setattr(admin_runtime, "is_ready", failure)
    response = await api.get("/api/v1/status", headers=HEADERS)
    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "internal-private-credential" not in response.text
