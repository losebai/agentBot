"""Laya 开关、请求授权及热更新契约。"""

import json

import httpx
import pytest
from pydantic import ValidationError

from agent_bot.config import PluginConfig, RuntimeConfig
from agent_bot.integrations.dispatch_ai import DispatchAiClient
from agent_bot.models import ChatMessage, DispatchAiRequest
from agent_bot.settings import runtime_config_to_plain_values


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,chat_tools", [(True, True), (False, True), (True, False)])
async def test_laya_is_declared_only_for_enabled_chat_tool_requests(
    enabled: bool, chat_tools: bool,
) -> None:
    """开关只影响 Chat Agent 的附加服务，不改变其他工具和普通聊天入口。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        """检查真实客户端生成的出站契约。"""
        payload = json.loads(request.content)
        endpoint = "chatAgentOne" if chat_tools else "chatOne"
        assert request.url.path.endswith(f"/chat/{endpoint}")
        if enabled and chat_tools:
            assert payload["mcpServices"] == ["laya"]
        else:
            assert "mcpServices" not in payload
        if chat_tools:
            assert payload["tools"] == ["memory", "web_search"]
            assert payload["toolSessionToken"] == "a" * 40
        assert "mcpUrl" not in payload and "DISPATCH_LAYA_TOKEN" not in payload
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "判断完成"}})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai", timeout_seconds=3,
        laya_enabled=enabled, transport=httpx.MockTransport(handler),
    )
    try:
        reply = await client.complete(
            model="model", session_id="session", prompt="判断退款意图", request_id="laya-test",
            messages=[ChatMessage(role="user", content="请退款")],
            use_agent=False, chat_tools=("memory",) if chat_tools else (),
            tool_session_token="a" * 40 if chat_tools else None,
        )
    finally:
        await client.aclose()
    assert reply == "判断完成"


@pytest.mark.asyncio
async def test_laya_decision_does_not_include_general_mcp_services() -> None:
    """专用 Laya 决策必须保持精确服务授权，不能继承普通 Agent 的通用 MCP。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        """验证专用决策请求只声明 Laya。"""
        payload = json.loads(request.content)
        assert request.url.path.endswith("/chat/chatAgentOne")
        assert payload["mcpServices"] == ["laya"]
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "判断完成"}})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        mcp_services=("windows-mcp",),
        laya_enabled=True,
        transport=httpx.MockTransport(handler),
    )
    try:
        reply = await client.complete(
            model="model",
            session_id="session",
            prompt="判断退款意图",
            request_id="laya-only-test",
            messages=[ChatMessage(role="user", content="请退款")],
            use_web_search=False,
            tool_session_token="a" * 40,
            laya_decision=True,
        )
    finally:
        await client.aclose()

    assert reply == "判断完成"


def test_laya_cannot_bypass_chat_tool_session_authorization() -> None:
    """仅声明 Laya 不能构成一个授权的 Chat Agent 请求。"""
    with pytest.raises(ValidationError, match="authorized Chat tool session"):
        DispatchAiRequest.model_validate({
            "prompt": "退款", "model": "model", "sessionId": "session",
            "requestsId": "request", "messages": [], "mcpServices": ["laya"],
        })


def test_laya_configuration_requires_chat_mcp_and_defaults_to_disabled() -> None:
    """避免开启功能后网关却无法创建 Chat 工具会话。"""
    values = {
        "onebot_v11_access_token": "test-onebot-token-with-enough-entropy",
        "dispatch_chat_session_secret": "test-session-secret-with-enough-entropy",
    }
    assert PluginConfig.model_validate(values).dispatch_chat_laya_enabled is False
    with pytest.raises(ValidationError, match="Laya requires Chat MCP"):
        PluginConfig.model_validate({**values, "dispatch_chat_laya_enabled": True})
    enabled = PluginConfig.model_validate({
        **values, "dispatch_chat_laya_enabled": True, "dispatch_chat_mcp_enabled": True,
    })
    assert enabled.dispatch_chat_laya_enabled is True


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_runtime_reload_applies_laya_selection_to_next_request(chat_plugin, enabled) -> None:
    """数据库配置热更新后，新客户端的出站请求立即使用新开关。"""
    current = chat_plugin._runtime_state
    values = runtime_config_to_plain_values(current.config)
    values.update(dispatch_chat_mcp_enabled=True, dispatch_chat_laya_enabled=not enabled)
    before = RuntimeConfig.model_validate(values)
    values["dispatch_chat_laya_enabled"] = enabled
    candidate = RuntimeConfig.model_validate(values)
    effective, restart_fields = chat_plugin._effective_runtime_config(candidate, before)
    assert restart_fields == set()
    replacement = chat_plugin._build_runtime_state(effective, current)

    async def handler(request: httpx.Request) -> httpx.Response:
        """在 HTTP 边界确认配置传入实际客户端。"""
        payload = json.loads(request.content)
        assert payload.get("mcpServices") == (["laya"] if enabled else None)
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "完成"}})

    original_http = replacement.ai_client.java_client._http
    replacement.ai_client.java_client._http = httpx.AsyncClient(
        base_url="http://ai.test/dispatch-ai/", transport=httpx.MockTransport(handler),
    )
    await original_http.aclose()
    try:
        await replacement.ai_client.complete(
            model="model", session_id="session", prompt="退款", request_id="reload-laya",
            messages=[ChatMessage(role="user", content="退款")], chat_tools=("memory",),
            tool_session_token="a" * 40, use_web_search=False,
        )
    finally:
        await chat_plugin._close_runtime_state(replacement)
