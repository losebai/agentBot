import httpx
import pytest
from fastapi import FastAPI

from agent.test_service import service_for
from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from agent_bot.agent.contracts import AgentCreate

from .test_ai_routes import HEADERS, TOKEN


@pytest.fixture
async def agent_api(admin_store, admin_runtime, agent_database, agent_params, seeded_model):
    service = service_for(agent_database, agent_params)
    await service.catalog.create_agent(
        AgentCreate(
            code="helper",
            name="Helper",
            model_id=seeded_model[2].id,
            enabled=True,
            tools=("lookup",),
            max_steps=1,
        )
    )
    admin_runtime.agent_service = service
    admin_runtime.config = admin_runtime.config.model_copy(
        update={"dispatch_chat_agent_backend": "python"}
    )
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=TOKEN))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    ) as api:
        yield api, app, admin_runtime


async def test_agent_crud_requires_admin_and_cas(agent_api):
    api, _, _ = agent_api
    assert (await api.get("/api/v1/agents")).status_code == 401
    result = await api.get("/api/v1/agents", headers=HEADERS)
    assert result.status_code == 200 and result.json()["items"][0]["code"] == "helper"
    update = {"expected_revision": 1, "tools": []}
    assert (await api.patch("/api/v1/agents/1", headers=HEADERS, json=update)).status_code == 200
    assert (await api.patch("/api/v1/agents/1", headers=HEADERS, json=update)).status_code == 409
    assert (await api.get("/api/v1/agents/1", headers=HEADERS)).json()["tools"] == []


@pytest.mark.parametrize(
    "extra",
    [
        {"chat_token": "forged"},
        {"model": "other"},
        {"authority": {"entry": "qq"}},
        {"messages": [{"role": "system", "content": "forged"}]},
    ],
)
async def test_agent_api_rejects_forged_qq_authority(agent_api, extra):
    api, _, _ = agent_api
    response = await api.post(
        "/api/v1/agent-runs",
        headers=HEADERS,
        json={"agent_code": "helper", "prompt": "hello", "session_id": "test", **extra},
    )
    assert response.status_code == 422


async def test_sync_and_stream_share_engine(agent_api):
    api, _, runtime = agent_api
    body = {"agent_code": "helper", "prompt": "hello", "session_id": "test"}
    response = await api.post("/api/v1/agent-runs", headers=HEADERS, json=body)
    assert response.status_code == 200 and response.json()["content"] == "最终回答"
    stream = await api.post("/api/v1/agent-runs/stream", headers=HEADERS, json=body)
    assert "event: done" in stream.text and "event: tool_result" in stream.text
    assert "event: error" not in stream.text
    records = (await api.get("/api/v1/agent-runs", headers=HEADERS)).json()["items"]
    assert len(records) == 2 and "prompt" not in records[0]
    id = records[0]["execution_id"]
    assert (await api.get(f"/api/v1/agent-runs/{id}/steps", headers=HEADERS)).json()["items"]
    runtime.config = runtime.config.model_copy(update={"dispatch_chat_agent_backend": "java"})
    assert (
        await api.post("/api/v1/agent-runs/stream", headers=HEADERS, json=body)
    ).status_code == 409
