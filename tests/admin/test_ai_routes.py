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
        CredentialResolver(environ={}),
        {
            "ollama": OllamaProvider(
                ProviderTransport(
                    httpx.MockTransport(
                        lambda req: (
                            httpx.Response(200, stream=ByteStream(stream_body("ollama")))
                            if __import__("json").loads(req.content)["stream"]
                            else httpx.Response(
                                200,
                                json={"message": {"content": "真实 mock 协议回复"}, "done": True},
                            )
                        )
                    )
                )
            )
        },
    )
    admin_runtime.ai_service = service
    admin_runtime.config = admin_runtime.config.model_copy(
        update={"dispatch_chat_plain_ai_backend": "python", "dispatch_chat_model": "local"}
    )
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


async def test_sync_stream_and_record_queries(python_api):
    api, _, runtime = python_api
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
    runtime.config = runtime.config.model_copy(update={"dispatch_chat_plain_ai_backend": "java"})
    assert (await api.post("/api/v1/chat/stream", headers=HEADERS, json=body)).status_code == 409
