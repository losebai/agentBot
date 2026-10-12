"""Exercise real ASGI management routes with isolated persistence and HTTP transports."""

import json

import httpx
import pytest
from fastapi import FastAPI

from agent_bot.integrations.dispatch_ai import DispatchAiClient

TOKEN = "offline-admin-token-longer-than-thirty-two-characters"
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


async def test_status_without_connected_bots_is_ready(api):
    response = await api.get("/api/v1/status", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["ready"] is True
    assert response.json()["bot_count"] == 0
    assert response.json()["ai_backend"] == "java-compatibility"
    assert (await api.get("/api/v1/bots", headers=HEADERS)).json() == []


async def test_settings_conflict_and_secret_validation_are_safe(api):
    response = await api.patch(
        "/api/v1/settings",
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "values": {"port": 8765},
        },
    )
    assert response.status_code == 200
    assert response.json()["restart_required_fields"] == ["port"]
    assert response.json()["database_revision"] == 2
    assert response.json()["applied_revision"] == 1
    conflict = await api.patch(
        "/api/v1/settings",
        headers=HEADERS,
        json={
            "expected_revision": 1,
            "values": {"port": 9000},
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


async def test_chat_only_calls_plain_ai_and_returns_real_reply(api, admin_runtime):
    def upstream(request):
        assert request.url.path == "/dispatch-ai/chat/chatOne"
        payload = json.loads(request.content)
        assert not payload.get("tools") and not payload.get("mcpServices")
        assert not payload.get("toolSessionToken")
        assert payload["messages"][-1]["content"] == "hello"
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "actual reply"}})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=10,
        transport=httpx.MockTransport(upstream),
    )
    admin_runtime.client = client
    try:
        response = await api.post(
            "/api/v1/chat", headers=HEADERS, json={"prompt": "hello", "session_id": "web-test"}
        )
        assert response.status_code == 200, response.text
        assert response.json()["content"] == "actual reply"
    finally:
        await client.aclose()


async def test_ai_timeout_is_safe_and_not_a_success(api, admin_runtime):
    def timeout(request):
        raise httpx.ReadTimeout("private-upstream-token", request=request)

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=1,
        transport=httpx.MockTransport(timeout),
    )
    admin_runtime.client = client
    try:
        response = await api.post(
            "/api/v1/chat", headers=HEADERS, json={"prompt": "hello", "session_id": "web-test"}
        )
        assert response.status_code == 503
        assert "private-upstream-token" not in response.text
    finally:
        await client.aclose()


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
