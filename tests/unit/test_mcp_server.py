from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient
from mcp.types import LATEST_PROTOCOL_VERSION
from starlette.applications import Starlette
from workflow_fakes import TextWorkflowServiceStub

from agent_bot.integrations.mcp import ChatMcpServer, ToolSessionTokenVerifier
from agent_bot.services.chat_tools import (
    ChatToolContext,
    ChatToolDefinition,
    ChatToolRegistry,
    build_chat_tool_registry,
)
from agent_bot.services.tool_sessions import ToolSessionExpiredError, ToolSessionStore
from agent_bot.services.workflow_execution import (
    GeneratedWorkflowImage,
    WorkflowImageCollector,
    WorkflowSummary,
)


@pytest.mark.asyncio
async def test_list_tools_is_filtered_by_the_bearer_session_allow_list() -> None:
    store = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    token = await store.create(
        ChatToolContext(
            session_id="private:1",
            user_id="1",
            allowed_tools=frozenset({"naraka_search"}),
        )
    )
    server = ChatMcpServer(
        store=store,
        registry=build_chat_tool_registry(),
        allowed_hosts=["localhost:*"],
    )

    result = await server.list_tools(token)

    assert [tool.name for tool in result.tools] == ["naraka_search"]
    assert result.tools[0].input_schema["additionalProperties"] is False


@pytest.mark.asyncio
async def test_token_verifier_rejects_unknown_or_revoked_tokens() -> None:
    store = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    context = ChatToolContext(
        session_id="private:1",
        user_id="1",
        allowed_tools=frozenset({"naraka_search"}),
    )
    token = await store.create(context)
    verifier = ToolSessionTokenVerifier(store)

    access_token = await verifier.verify_token(token)

    assert access_token is not None
    assert access_token.token == token
    assert await verifier.verify_token("unknown-token") is None
    await store.revoke(token)
    assert await verifier.verify_token(token) is None


@pytest.mark.asyncio
async def test_call_tool_rechecks_authorization_and_returns_json_text_content() -> None:
    async def echo(arguments: dict[str, object], context: ChatToolContext) -> object:
        return {"session": context.session_id, "value": arguments.get("value")}

    registry = ChatToolRegistry(
        [
            ChatToolDefinition(
                name="echo",
                description="echo",
                input_schema={"type": "object", "additionalProperties": False},
                handler=echo,
            )
        ]
    )
    store = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    allowed_token = await store.create(
        ChatToolContext(
            session_id="group:123",
            user_id="1",
            allowed_tools=frozenset({"echo"}),
        )
    )
    denied_token = await store.create(
        ChatToolContext(
            session_id="group:456",
            user_id="2",
            allowed_tools=frozenset(),
        )
    )
    server = ChatMcpServer(store=store, registry=registry, allowed_hosts=["localhost:*"])

    success = await server.call_tool(allowed_token, "echo", {"value": 7})
    denied = await server.call_tool(denied_token, "echo", {"value": 7})

    assert success.is_error is False
    assert json.loads(success.content[0].text) == {"session": "group:123", "value": 7}
    assert denied.is_error is True
    assert json.loads(denied.content[0].text)["code"] == "FORBIDDEN"


@pytest.mark.asyncio
async def test_internal_tool_errors_are_sanitized() -> None:
    async def broken(arguments: dict[str, object], context: ChatToolContext) -> object:
        raise RuntimeError("secret-upstream-response")

    registry = ChatToolRegistry(
        [
            ChatToolDefinition(
                name="broken",
                description="broken",
                input_schema={"type": "object"},
                handler=broken,
            )
        ]
    )
    store = ToolSessionStore(ttl_seconds=30, max_sessions=1)
    token = await store.create(
        ChatToolContext(
            session_id="private:1",
            user_id="1",
            allowed_tools=frozenset({"broken"}),
        )
    )
    server = ChatMcpServer(store=store, registry=registry, allowed_hosts=["localhost:*"])

    result = await server.call_tool(token, "broken", {})

    assert result.is_error is True
    assert "secret-upstream-response" not in result.content[0].text
    assert json.loads(result.content[0].text)["code"] == "INTERNAL"


@pytest.mark.asyncio
async def test_validation_error_exposes_field_names_but_not_argument_values() -> None:
    store = ToolSessionStore(ttl_seconds=30, max_sessions=1)
    token = await store.create(
        ChatToolContext(
            session_id="private:1",
            user_id="1",
            allowed_tools=frozenset({"local_workflow_generate_image"}),
        )
    )
    server = ChatMcpServer(
        store=store,
        registry=build_chat_tool_registry(),
        allowed_hosts=["localhost:*"],
    )

    result = await server.call_tool(
        token,
        "local_workflow_generate_image",
        {"prompt": "private raw prompt"},
    )

    payload = json.loads(result.content[0].text)
    assert result.is_error is True
    assert payload["code"] == "INVALID_ARGUMENT"
    assert "positive_prompt" in payload["message"]
    assert "prompt" in payload["message"]
    assert "private raw prompt" not in payload["message"]


@pytest.mark.asyncio
async def test_solon_session_context_does_not_enter_workflow_arguments() -> None:
    class WorkflowService(TextWorkflowServiceStub):
        async def generate(self, **_arguments: str) -> GeneratedWorkflowImage:
            return GeneratedWorkflowImage(media_type="image/png", data=b"image")

    store = ToolSessionStore(ttl_seconds=30, max_sessions=1)
    collector = WorkflowImageCollector(max_calls=1, max_images=1, max_total_bytes=1024)
    token = await store.create(
        ChatToolContext(
            session_id="private:1",
            user_id="1",
            allowed_tools=frozenset({"local_workflow_generate_image"}),
            workflow_service=WorkflowService(),
            workflow_collector=collector,
        )
    )
    server = ChatMcpServer(
        store=store,
        registry=build_chat_tool_registry(),
        allowed_hosts=["localhost:*"],
    )

    result = await server.call_tool(
        token,
        "local_workflow_generate_image",
        {
            "positive_prompt": "cute anime girl",
            "workflow_key": "workflow-98926948bfe1",
            "start_reply": "我来画。",
            "negative_prompt": "blurry",
            "__sessionId": "solon-internal-session",
        },
    )

    assert result.is_error is False
    assert len(collector.images) == 1


@pytest.mark.asyncio
async def test_empty_raw_arguments_does_not_break_zero_argument_workflow_list() -> None:
    class WorkflowService:
        async def list_workflows(self) -> tuple[WorkflowSummary, ...]:
            return (
                WorkflowSummary(
                    key="workflow-98926948bfe1",
                    name="测试工作流",
                    description="用于验证 MCP 工作流列表",
                ),
            )

    store = ToolSessionStore(ttl_seconds=30, max_sessions=1)
    token = await store.create(
        ChatToolContext(
            session_id="private:1",
            user_id="1",
            allowed_tools=frozenset({"local_workflow_list"}),
            workflow_service=WorkflowService(),
        )
    )
    server = ChatMcpServer(
        store=store,
        registry=build_chat_tool_registry(),
        allowed_hosts=["localhost:*"],
    )

    result = await server.call_tool(
        token,
        "local_workflow_list",
        {"raw_arguments": "", "__sessionId": "solon-internal-session"},
    )

    assert result.is_error is False
    workflow = json.loads(result.content[0].text)["workflows"][0]
    assert workflow == {
        "key": "workflow-98926948bfe1",
        "name": "测试工作流",
        "description": "用于验证 MCP 工作流列表",
        "workflow_type": "text_to_image",
        "requires_image": False,
    }


@pytest.mark.asyncio
async def test_direct_handlers_reject_expired_sessions() -> None:
    store = ToolSessionStore(ttl_seconds=30, max_sessions=1)
    server = ChatMcpServer(
        store=store,
        registry=build_chat_tool_registry(),
        allowed_hosts=["localhost:*"]
    )

    with pytest.raises(ToolSessionExpiredError):
        await server.list_tools("not-a-session")


@pytest.mark.asyncio
async def test_streamable_http_requires_bearer_and_serves_filtered_tools() -> None:
    store = ToolSessionStore(ttl_seconds=30, max_sessions=1)
    token = await store.create(
        ChatToolContext(
            session_id="private:1",
            user_id="1",
            allowed_tools=frozenset({"naraka_search"}),
        )
    )
    server = ChatMcpServer(
        store=store,
        registry=build_chat_tool_registry(),
        allowed_hosts=["localhost:*"],
    )
    app = Starlette()
    app.mount("/mcp", server.app)
    transport = ASGITransport(app=app)
    initialize_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": LATEST_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "test-client", "version": "1.0"},
        },
    }

    await server.start()
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://localhost:8080",
        ) as client:
            unauthenticated = await client.post("/mcp/", json=initialize_request)
            assert unauthenticated.status_code == 401

            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json, text/event-stream",
            }
            initialized = await client.post(
                "/mcp/",
                headers=headers,
                json=initialize_request,
            )
            assert initialized.status_code == 200
            headers["Mcp-Session-Id"] = initialized.headers["mcp-session-id"]

            notification = await client.post(
                "/mcp/",
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            assert notification.status_code == 202
            tools = await client.post(
                "/mcp/",
                headers=headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            assert tools.status_code == 200
            assert [tool["name"] for tool in tools.json()["result"]["tools"]] == [
                "naraka_search"
            ]
    finally:
        await server.stop()
