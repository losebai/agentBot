from unittest.mock import AsyncMock

import pytest

from agent_bot.agent.errors import AgentError
from agent_bot.ai.chat_errors import (
    ChatAiProtocolError,
    ChatAiRejectedError,
    ChatAiUnavailableError,
)
from agent_bot.ai.routing import RoutingAiClient
from agent_bot.models import LlmRequestOptions


@pytest.mark.parametrize("code", ["AGENT_TIMEOUT", "AI_TIMEOUT", "AGENT_TOOL_SESSION_EXPIRED"])
async def test_qq_routes_interrupted_body_without_calling_another_model(code):
    """超时已有正文直接返回 QQ 队列，不再次生成或解释错误。"""
    plain, agent = AsyncMock(), AsyncMock()
    error = AgentError(code, status_code=504)
    error.partial_content = "已生成内容"
    agent.complete.side_effect = error
    client = RoutingAiClient(
        plain, agent_service=agent, timeout_seconds=30,
        default_options=LlmRequestOptions(), mcp_services=(),
    )
    assert await client.complete(
        model="local", session_id="s", prompt="hi", messages=(), request_id="r",
        use_agent=True, use_web_search=False,
    ) == "已生成内容"
    agent.complete.assert_awaited_once()
    plain.complete.assert_not_awaited()


@pytest.mark.parametrize("use_agent", [False, True])
@pytest.mark.parametrize(
    "code,exception",
    [
        ("AI_UPSTREAM_UNAVAILABLE", ChatAiUnavailableError),
        ("AI_UPSTREAM_AUTH_REJECTED", ChatAiRejectedError),
        ("AI_UPSTREAM_REJECTED", ChatAiRejectedError),
    ],
)
async def test_upstream_http_failure_keeps_its_category(use_agent, code, exception) -> None:
    """HTTP 5xx 的稳定错误码表示上游不可用，不能因对外状态为 502 误判为拒绝。"""
    plain, agent = AsyncMock(), AsyncMock()
    service = agent if use_agent else plain
    service.complete.side_effect = AgentError(code, status_code=502)
    client = RoutingAiClient(
        plain, agent_service=agent, timeout_seconds=30,
        default_options=LlmRequestOptions(), mcp_services=(),
    )
    try:
        with pytest.raises(exception) as caught:
            await client.complete(
                model="local", session_id="s", prompt="查看我的桌面", messages=(), request_id="r",
                use_agent=use_agent, use_web_search=False,
            )
        assert caught.value.error_code == code
        assert caught.value.status_code == 502
        service.complete.assert_awaited_once()
        (plain if use_agent else agent).complete.assert_not_awaited()
    finally:
        await client.aclose()


async def test_python_failure_never_falls_back():
    plain, agent = AsyncMock(), AsyncMock()
    agent.complete.side_effect = AgentError("AGENT_TIMEOUT", status_code=504)
    client = RoutingAiClient(
        plain,
        agent_service=agent,
        agent_code="helper",
        timeout_seconds=30,
        default_options=LlmRequestOptions(),
        mcp_services=(),
    )
    with pytest.raises(ChatAiUnavailableError):
        await client.complete(
            model="local",
            session_id="s",
            prompt="hi",
            messages=(),
            request_id="r",
            use_agent=True,
            use_web_search=False,
        )
    request, authority = agent.complete.call_args.args
    assert request.agent_code == "helper" and request.model is None
    assert authority.allowed_tools == ("knowledge_search", "material_search", "material_get")
    await client.aclose()
    agent.aclose.assert_awaited_once()
    plain.aclose.assert_awaited_once()


async def test_agent_model_failure_keeps_completed_tool_count_for_qq():
    """路由转换错误时保留成功回执数，不重试模型或丢失已执行事实。"""
    plain, agent = AsyncMock(), AsyncMock()
    agent.complete.side_effect = AgentError("AI_RESPONSE_INVALID", completed_tool_calls=2)
    client = RoutingAiClient(
        plain, agent_service=agent, timeout_seconds=30,
        default_options=LlmRequestOptions(), mcp_services=(),
    )
    try:
        with pytest.raises(ChatAiProtocolError) as caught:
            await client.complete(
                model="local", session_id="s", prompt="查看电脑状态", messages=(), request_id="r",
                use_agent=True, use_web_search=False,
            )
        assert caught.value.completed_tool_calls == 2
        assert caught.value.error_code == "AI_RESPONSE_INVALID"
        agent.complete.assert_awaited_once()
        plain.complete.assert_not_awaited()
    finally:
        await client.aclose()


@pytest.mark.parametrize("names", [(), ("memory",), ("memory", "local_workflow_generate_image")])
async def test_qq_precheck_uses_same_route_and_agent_model(
    agent_database, agent_params, seeded_model, names
):
    """无工具聊天和工具聊天均预检并使用 Agent 模型，不依赖失效的聊天别名。"""
    from agent_bot.agent.contracts import AgentCreate
    from agent_bot.ai.routing import RoutedModelGuard
    from agent_bot.model_router import ContentModelRouter
    from agent_bot.services.chat import ChatService
    from agent_bot.state import ConversationStore

    from .test_service import service_for

    agent = service_for(agent_database, agent_params)
    await agent.catalog.create_agent(
        AgentCreate(
            code="helper",
            name="helper",
            enabled=True,
            model_id=seeded_model[2].id,
            tools=("lookup",),
            max_steps=1,
        )
    )
    plain = AsyncMock()
    client = RoutingAiClient(
        plain,
        agent_service=agent,
        agent_code="helper",
        agent_max_steps=1,
        timeout_seconds=30,
        default_options=LlmRequestOptions(),
        mcp_services=(),
    )
    guard = RoutedModelGuard(seeded_model[0], agent_catalog=agent.catalog, agent_code="helper")
    chat = ChatService(
        client=client,
        model_guard=guard,
        model_router=ContentModelRouter(
            text_model="local",
            vision_model="vision",
            image_model="image",
            audio_model="audio",
        ),
        store=ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60),
        system_prompt="system",
        max_input_chars=100,
        cooldown_seconds=0,
        max_concurrent_requests=2,
        web_search_enabled=False,
    )
    try:
        async def deliver(answer):
            """模拟成功投递，不建立 QQ 连接。"""
            return answer

        await chat.validate_queued("hello", chat_tools=names)
        answer = await chat.respond(
            "session", "hello", delivery=deliver, chat_tools=names,
            tool_session_token="offline" if names else None,
        )
        assert answer == "最终回答"
        assert agent.providers["openai-chat"].calls[0].resolved.model.alias == "local-model"
        plain.complete.assert_not_awaited()
    finally:
        await chat.aclose()
        await client.aclose()
