from __future__ import annotations

import json

import httpx
import pytest

from agent_bot.integrations.dispatch_ai import DispatchAiClient, DispatchAiProtocolError
from agent_bot.models import ChatMessage


@pytest.mark.asyncio
async def test_memory_and_existing_tools_share_the_authenticated_chat_request() -> None:
    """记忆通过现有 Chat Agent 协议传输，最终回答仍解析为普通正文。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        """验证只携带工具令牌，不暴露数据库归属参数。"""
        payload = json.loads(request.content)
        assert request.url.path.endswith("/chat/chatAgentOne")
        assert payload["tools"] == ["memory", "mention_member"]
        assert payload["toolSessionToken"] == "a" * 40
        assert "memoryIdentity" not in payload
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "你喜欢甜点"}})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai", timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        reply = await client.complete(
            model="model", session_id="group:1", prompt="我喜欢什么",
            messages=[ChatMessage(role="user", content="我喜欢什么")], request_id="memory-1",
            chat_tools=("memory", "mention_member"), tool_session_token="a" * 40,
            use_web_search=False,
        )
    finally:
        await client.aclose()
    assert reply == "你喜欢甜点"


@pytest.mark.asyncio
async def test_image_edit_request_keeps_multimodal_input_and_workflow_tools():
    """图片仍走现有 input 协议；工具参数中不能出现 Comfy 路径。"""
    calls = []

    async def handler(request):
        calls.append(request)
        if request.method == "GET":
            assert request.url.host == "gchat.qpic.cn"
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nsource",
                                  headers={"content-type": "image/png"})
        payload = json.loads(request.content)
        assert payload["tools"] == ["local_workflow_list", "local_workflow_generate_image"]
        assert payload["input"][-1]["content"][0]["type"] == "input_text"
        assert payload["input"][-1]["content"][1]["type"] == "input_image"
        assert payload["input"][-1]["content"][1]["image_url"].startswith("data:image/png;")
        assert "workflowRequest" not in payload and "filename" not in payload
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "我来修改。"}})

    client = DispatchAiClient(base_url="http://ai.test/dispatch-ai", timeout_seconds=3,
                             transport=httpx.MockTransport(handler))
    try:
        await client.complete(
            model="vision", session_id="group:1", prompt="改蓝色", request_id="i2i",
            messages=[ChatMessage(role="user", content="改蓝色")],
            image_urls=("https://gchat.qpic.cn/source",),
            chat_tools=("local_workflow_list", "local_workflow_generate_image"),
            tool_session_token="a" * 40, use_web_search=False,
        )
    finally:
        await client.aclose()
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_tool_request_rejects_empty_final_text() -> None:
    """工具存在也必须返回可发送的最终正文。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        """只验证回复工具仍通过受限 Agent 接口传输。"""
        assert request.url.path.endswith("/chat/chatAgentOne")
        assert json.loads(request.content)["tools"] == ["mention_member"]
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": ""}})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai", timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DispatchAiProtocolError, match="empty reply"):
            await client.complete(
                model="model", session_id="group:1", prompt="不用回复",
                messages=[ChatMessage(role="user", content="不用回复")], request_id="silent",
                chat_tools=("mention_member",), tool_session_token="a" * 40,
                use_web_search=False,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_posts_only_chat_tools_and_session_token_to_dedicated_endpoint() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url == "http://ai.test/dispatch-ai/chat/chatAgentOne"
        assert payload["tools"] == ["naraka_query", "mention_member"]
        assert payload["toolSessionToken"] == "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
        assert payload["maxSteps"] == 3
        assert "mcp" not in payload
        assert "mcpUrl" not in payload
        assert "mcpServices" not in payload
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "完成"}})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        answer = await client.complete(
            model="model",
            session_id="group:1",
            prompt="查询并提醒",
            messages=[ChatMessage(role="user", content="查询并提醒")],
            request_id="req-1",
            chat_tools=("naraka_query", "mention_member"),
            tool_session_token="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            use_web_search=False,
        )
    finally:
        await client.aclose()

    assert answer == "完成"


@pytest.mark.asyncio
async def test_client_combines_general_mcp_with_chat_tools_and_laya() -> None:
    """通用 MCP、Chat MCP 和 Laya 使用同一受控 Chat Agent 请求。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path.endswith("/chat/chatAgentOne")
        assert payload["tools"] == ["memory"]
        assert payload["mcpServices"] == ["windows-mcp", "laya"]
        assert payload["toolSessionToken"] == "a" * 40
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "完成"}})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        mcp_services=("windows-mcp",),
        laya_enabled=True,
        transport=httpx.MockTransport(handler),
    )
    try:
        answer = await client.complete(
            model="model",
            session_id="private:1",
            prompt="读取窗口并记住结果",
            messages=[ChatMessage(role="user", content="读取窗口并记住结果")],
            request_id="general-mcp-chat-tools",
            chat_tools=("memory",),
            tool_session_token="a" * 40,
            use_web_search=False,
        )
    finally:
        await client.aclose()

    assert answer == "完成"


@pytest.mark.asyncio
@pytest.mark.parametrize("use_web_search", [True, False])
@pytest.mark.parametrize(
    ("prompt", "expected_network_tools"),
    [
        ("搜索新闻", ["web_search"]),
        ("看看 https://example.com/page", ["web_search", "read_url"]),
        ("看看 http://example.com/page", ["web_search", "read_url"]),
        ("看看 [网页](HTTPS://example.com/page)", ["web_search", "read_url"]),
        ("看看 ftp://example.com/file", ["web_search"]),
        ("看看 https://", ["web_search"]),
    ],
)
async def test_client_combines_search_with_chat_tools_on_dedicated_endpoint(
    prompt: str, expected_network_tools: list[str], use_web_search: bool,
) -> None:
    """联网开启时始终提供搜索，仅当前请求包含 HTTP URL 时提供网页读取。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        """检查出站工具列表，模拟模型直接回复。"""

        payload = json.loads(request.content)
        assert request.url.path.endswith("/chat/chatAgentOne")
        assert payload["tools"] == [
            "naraka_query", *(expected_network_tools if use_web_search else []),
        ]
        assert not payload.get("toolChoice")
        assert payload["toolSessionToken"] == "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "完成"}})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        answer = await client.complete(
            model="model",
            session_id="group:1",
            prompt=prompt,
            messages=[
                ChatMessage(role="system", content="资料网址 https://example.com/system"),
                ChatMessage(role="user", content="https://example.com/history"),
                ChatMessage(role="assistant", content="历史回答"),
                ChatMessage(role="user", content=prompt),
            ],
            request_id="req-search",
            chat_tools=("naraka_query",),
            tool_session_token="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
            use_web_search=use_web_search,
        )
    finally:
        await client.aclose()

    assert answer == "完成"


@pytest.mark.asyncio
async def test_client_requires_tools_and_token_together_before_network_access() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("network should not be called")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(ValueError, match="together"):
            await client.complete(
                model="model",
                session_id="group:1",
                prompt="查询",
                messages=[ChatMessage(role="user", content="查询")],
                request_id="req-1",
                chat_tools=("naraka_query",),
            )
    finally:
        await client.aclose()

    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(("agent_max_steps", "expected_steps"), [(None, 5), (1, 1), (4, 4)])
async def test_local_workflow_agent_applies_configured_steps(
    agent_max_steps: int | None, expected_steps: int,
) -> None:
    """显式普通聊天轮次覆盖画图预算，未设置时保留五轮。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["tools"] == [
            "local_workflow_list",
            "local_workflow_generate_image",
            "web_search",
        ]
        assert payload["maxSteps"] == expected_steps
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "完成"}})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        agent_max_steps=agent_max_steps,
        transport=httpx.MockTransport(handler),
    )
    try:
        await client.complete(
            model="model",
            session_id="private:1",
            prompt="生成三张图片",
            messages=[ChatMessage(role="user", content="生成三张图片")],
            request_id="req-workflow",
            chat_tools=("local_workflow_list", "local_workflow_generate_image"),
            tool_session_token="abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG",
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("with_image", [False, True])
async def test_committed_references_survive_text_and_multimodal_conversion(with_image):
    """引用原样通过 HTTP 传给 Java；当前问题不冒充已投递历史。"""
    async def handler(request):
        if request.method == "GET":
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\nsource",
                                  headers={"content-type": "image/png"})
        payload = json.loads(request.content)
        messages = payload["input" if with_image else "messages"]
        assert [m.get("historyRequestId") for m in messages] == ["old", "old", None]
        return httpx.Response(200, json={"code": 200, "data": {"replayContext": "ok"}})

    client = DispatchAiClient(base_url="http://ai.test/dispatch-ai", timeout_seconds=3,
                             transport=httpx.MockTransport(handler))
    try:
        await client.complete(
            model="test", session_id="s", prompt="next", request_id="new",
            messages=[ChatMessage(role="user", content="old", history_request_id="old"),
                      ChatMessage(role="assistant", content="done", history_request_id="old"),
                      ChatMessage(role="user", content="next")],
            image_urls=("https://gchat.qpic.cn/source",) if with_image else (),
            chat_tools=("memory",), tool_session_token="a" * 40, use_web_search=False,
        )
    finally:
        await client.aclose()
