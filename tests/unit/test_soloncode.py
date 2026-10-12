"""远程 SolonCode 工具的配置、会话授权、请求与后台结果投递契约。"""

import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

from agent_bot.config import RuntimeConfig
from agent_bot.integrations.dispatch_ai import DispatchAiClient
from agent_bot.integrations.onebot.tooling import ChatToolSessionManager
from agent_bot.models import ChatMessage, DispatchAiRequest
from agent_bot.runtime_reload import SettingsReloader
from agent_bot.services.chat.routing import ChatRouteSelector
from agent_bot.settings import VersionedSettings, runtime_config_to_plain_values


def config(**overrides):
    """生成不包含远程 SolonCode 凭据的网关配置。"""
    return RuntimeConfig(
        onebot_v11_access_token="test-only-onebot-token-with-enough-entropy",
        dispatch_chat_session_secret="test-only-session-secret-with-enough-entropy",
        **overrides,
    )


def test_requires_authenticated_job_delivery_and_defaults_disabled():
    """远程能力默认关闭，不能绕过已有服务鉴权与结果投递。"""
    assert not config().dispatch_chat_soloncode_enabled
    with pytest.raises(ValidationError, match="SolonCode requires"):
        config(dispatch_chat_soloncode_enabled=True)
    with pytest.raises(ValidationError, match="Job requires"):
        config(dispatch_chat_soloncode_enabled=True, dispatch_chat_job_enabled=True)


async def test_hot_refresh_updates_tool_authorization_and_persisted_setting():
    """数据库配置刷新后即时变更工具白名单与路由，关闭时移除授权。"""
    current = config(
        dispatch_chat_mcp_enabled=True, dispatch_chat_job_enabled=True,
        dispatch_chat_ai_token="test-only-service-token-0000000000000000",
    )
    manager = ChatToolSessionManager(
        config_provider=lambda: current, store_provider=lambda: object(),
        naraka_service_provider=lambda: None,
        group_member_refresher=AsyncMock(),
    )
    assert "soloncode" not in manager.available_group_tools()
    enabled = current.model_copy(update={"dispatch_chat_soloncode_enabled": True})

    async def apply(snapshot):
        """用已校验数据库快照替换测试中的配置引用。"""
        nonlocal current
        current = snapshot.config

    store = Mock(load_revision=Mock(return_value=2), load=Mock(
        return_value=VersionedSettings(config=enabled, revision=2),
    ))
    reloader = SettingsReloader(store, initial_revision=1, apply=apply)
    assert await reloader.refresh_once()
    assert runtime_config_to_plain_values(current)["dispatch_chat_soloncode_enabled"] is True
    assert "soloncode" in manager.available_group_tools()
    route = ChatRouteSelector().select(
        "让远程助手分析项目", available_tools=manager.available_group_tools(),
        web_search_enabled=False,
    )
    assert "soloncode" in route.chat_tools
    current = current.model_copy(update={"dispatch_chat_soloncode_enabled": False})
    assert "soloncode" not in manager.available_group_tools()


async def test_uses_authenticated_chat_endpoint_and_requires_tool_session():
    """请求复用 chatAgentOne，任务完成通过既有 job/results 返回。"""
    async def handler(request):
        """本地 HTTP 替身验证工具名、令牌和投递协议。"""
        assert request.headers["Authorization"] == "Bearer test-only-service-token"
        if request.method == "POST":
            assert request.url.path.endswith("/chat/chatAgentOne")
            payload = json.loads(request.content)
            assert payload["tools"] == ["soloncode"]
            assert payload["toolSessionToken"] == "a" * 40
            assert "remoteToken" not in payload
            return httpx.Response(200, json={"code": 200, "data": {"replayContext": "已提交任务"}})
        assert request.url.path.endswith("/job/results")
        return httpx.Response(200, json={"code": 200, "data": [{
            "id": "b" * 32, "botId": "90001", "sessionId": "session-a",
            "senderId": "10001", "scene": "private", "prompt": "分析代码",
            "status": "SUCCEEDED", "result": "远程分析结果", "runAt": "2026-10-01T00:00:00Z",
        }]})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai", timeout_seconds=3,
        token="test-only-service-token", transport=httpx.MockTransport(handler),
    )
    try:
        reply = await client.complete(
            model="test-model", session_id="session-a", prompt="分析代码", request_id="remote-1",
            messages=[ChatMessage(role="user", content="分析代码")],
            chat_tools=("soloncode",), tool_session_token="a" * 40, use_web_search=False,
        )
        assert reply == "已提交任务"
        assert (await client.pending_jobs("90001"))[0].result == "远程分析结果"
    finally:
        await client.aclose()
    with pytest.raises(ValidationError, match="tool session token"):
        DispatchAiRequest(
            prompt="分析代码", sessionId="session-a", model="test-model", requestsId="remote-1",
            messages=[ChatMessage(role="user", content="分析代码")], tools=("soloncode",),
        )
