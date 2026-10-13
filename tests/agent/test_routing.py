from unittest.mock import AsyncMock

import pytest

from agent_bot.agent.errors import AgentError
from agent_bot.ai.chat_errors import ChatAiUnavailableError
from agent_bot.ai.routing import RoutingAiClient
from agent_bot.models import LlmRequestOptions


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
    assert authority.allowed_tools == ()
    await client.aclose()
    agent.aclose.assert_awaited_once()
    plain.aclose.assert_awaited_once()


@pytest.mark.parametrize("mixed", [False, True])
async def test_qq_precheck_uses_same_route_and_agent_model(
    agent_database, agent_params, seeded_model, mixed
):
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
    guard = RoutedModelGuard(seeded_model[0])
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
        agent_enabled=True,
        web_search_enabled=False,
    )
    try:
        names = ("memory", "local_workflow_generate_image") if mixed else ("memory",)

        async def deliver(answer):
            return answer

        answer = await chat.respond(
            "session", "hello", delivery=deliver, chat_tools=names, tool_session_token="offline"
        )
        assert answer == "最终回答"
        assert agent.providers["openai-chat"].calls[0].resolved.model.alias == "local-model"
    finally:
        await chat.aclose()
        await client.aclose()
