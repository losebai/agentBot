import httpx
import pytest
from fastapi import FastAPI

from agent.test_service import service_for
from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from agent_bot.agent.contracts import AgentCreate, McpCreate

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


async def test_management_accepts_shared_model_roles(agent_api, media_model):
    """管理 API 可保存三种角色共用同一模型，不再返回重复模型错误。"""
    api, _, _ = agent_api
    model = media_model[2]
    response = await api.patch("/api/v1/agents/1", headers=HEADERS, json={
        "expected_revision": 1, "model_id": model.id,
        "vision_model_id": model.id, "fallback_model_id": model.id,
    })
    assert response.status_code == 200
    assert response.json()["vision_model_id"] == response.json()["fallback_model_id"] == model.id


@pytest.mark.parametrize("vision_id,expected_status,expected_code,expected_message", [
    (1, 422, "MEDIA_VISION_UNSUPPORTED", "请在“供应商与模型”中开启后重试"),
    (99999, 404, "AI_CONFIG_NOT_FOUND", "请刷新列表后重新选择"),
])
async def test_management_model_validation_explains_failure_and_preserves_config(
    agent_api, vision_id, expected_status, expected_code, expected_message,
):
    """能力不足或引用失效必须返回可操作中文原因，失败时不写入部分模型配置。"""
    api, _, _ = agent_api
    response = await api.patch("/api/v1/agents/1", headers=HEADERS, json={
        "expected_revision": 1, "vision_model_id": vision_id,
    })
    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert expected_message in response.json()["message"]
    saved = (await api.get("/api/v1/agents/1", headers=HEADERS)).json()
    assert saved["revision"] == 1 and saved["vision_model_id"] is None


async def test_mcp_tool_discovery_requires_admin_and_does_not_change_whitelist(agent_api):
    """读取完整工具目录不执行工具、不启用停用服务，也不改写已保存的工具集合。"""
    from agent.mcp_server import peer

    api, _, runtime = agent_api
    async with peer(tool_names=["Snapshot", "Click"]) as (url, state):
        server = await runtime.agent_service.catalog.create_mcp(McpCreate(
            code="windows", name="Windows", endpoint=url + "/mcp", enabled=False,
            allowed_tools=("Snapshot",),
        ))
        path = f"/api/v1/mcp-servers/{server.id}/tools"
        assert (await api.get(path)).status_code == 401
        assert state["calls"] == []
        response = await api.get(path, headers=HEADERS)
        assert response.status_code == 200
        assert {tool["name"] for tool in response.json()} == {"Snapshot", "Click"}
        saved = await runtime.agent_service.catalog.get_mcp(server.id)
        assert saved.allowed_tools == ("Snapshot",) and not saved.enabled
        assert saved.revision == server.revision
        assert "tools/call" not in state["calls"]


async def test_mcp_all_tools_can_be_disabled_without_connecting(agent_api):
    """空集合表示关闭全部工具，保留服务配置，并允许重新获取服务端工具。"""
    from agent.mcp_server import peer
    from agent_bot.agent.mcp.client import McpConnector
    from agent_bot.agent.mcp.credentials import McpCredentials

    api, _, runtime = agent_api
    async with peer() as (url, state):
        server = await runtime.agent_service.catalog.create_mcp(McpCreate(
            code="windows", name="Windows", endpoint=url + "/mcp", enabled=True,
            allowed_tools=("*",), required=True,
        ))
        response = await api.patch(f"/api/v1/mcp-servers/{server.id}", headers=HEADERS, json={
            "expected_revision": server.revision, "allowed_tools": [],
        })
        assert response.status_code == 200 and response.json()["allowed_tools"] == []
        saved = await runtime.agent_service.catalog.get_mcp(server.id)
        async with McpConnector(McpCredentials()).open((saved,), remaining=lambda: 5) as conn:
            assert conn.tools == () and conn.warnings == ()
        assert state["calls"] == []
        tools = await api.get(f"/api/v1/mcp-servers/{server.id}/tools", headers=HEADERS)
        assert tools.status_code == 200 and tools.json()[0]["name"] == "lookup"


async def test_mcp_management_can_list_more_than_execution_tool_limit(agent_api):
    """管理目录允许读取超过 32 项的完整工具集，用户才能选择执行预算内的工具。"""
    from agent.mcp_server import peer

    api, _, runtime = agent_api
    names = [f"tool_{index}" for index in range(40)]
    async with peer(tool_names=names) as (url, state):
        server = await runtime.agent_service.catalog.create_mcp(McpCreate(
            code="many", name="工具目录", endpoint=url + "/mcp", allowed_tools=("tool_0",),
        ))
        response = await api.get(f"/api/v1/mcp-servers/{server.id}/tools", headers=HEADERS)
        assert response.status_code == 200
        assert [tool["name"] for tool in response.json()] == names
        assert "tools/call" not in state["calls"]


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
    api, _, _runtime = agent_api
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
