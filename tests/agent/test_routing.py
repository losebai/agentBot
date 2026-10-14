from unittest.mock import AsyncMock

import pytest

from agent_bot.agent.errors import AgentError
from agent_bot.ai.chat_errors import ChatAiUnavailableError
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
    assert authority.allowed_tools == ("knowledge_search",)
    await client.aclose()
    agent.aclose.assert_awaited_once()
    plain.aclose.assert_awaited_once()


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
