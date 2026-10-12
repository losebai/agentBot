from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.agent.errors import AgentError
from agent_bot.ai.routing import AiRouteRequest, RoutingAiClient, select_route
from agent_bot.integrations.dispatch_ai import DispatchAiUnavailableError
from agent_bot.models import LlmRequestOptions


@pytest.mark.parametrize(
    "route_request,backend,capability",
    [
        (AiRouteRequest(), "python", "ordinary-text"),
        (AiRouteRequest(agent=True), "python", "agent"),
        (AiRouteRequest(web=True), "python", "agent"),
        (AiRouteRequest(chat_tools=("memory",)), "python", "agent"),
        (AiRouteRequest(chat_tools=("memory", "local_workflow_generate_image")), "java", "legacy"),
        (AiRouteRequest(mcp_services=("laya",)), "java", "legacy"),
        (AiRouteRequest(images=True), "java", "legacy"),
    ],
)
def test_capability_route(route_request, backend, capability):
    result = select_route(route_request, plain_backend="python", agent_backend="python")
    assert (result.backend, result.capability) == (backend, capability)


async def test_python_failure_never_falls_back():
    java, plain, agent = AsyncMock(), AsyncMock(), AsyncMock()
    agent.complete.side_effect = AgentError("AGENT_TIMEOUT", status_code=504)
    client = RoutingAiClient(
        java,
        plain,
        plain_backend="python",
        agent_backend="python",
        agent_service=agent,
        agent_code="helper",
        timeout_seconds=30,
        default_options=LlmRequestOptions(),
        mcp_services=(),
        laya_enabled=False,
    )
    with pytest.raises(DispatchAiUnavailableError):
        await client.complete(
            model="java-model",
            session_id="s",
            prompt="hi",
            messages=(),
            request_id="r",
            use_agent=True,
            use_web_search=False,
        )
    java.complete.assert_not_called()
    request, authority = agent.complete.call_args.args
    assert request.agent_code == "helper" and request.model is None
    assert authority.allowed_tools == ()
    await client.aclose()
    agent.aclose.assert_awaited_once()
    plain.aclose.assert_awaited_once()
    java.aclose.assert_awaited_once()


def test_defaults_keep_java_without_agent_tables():
    client = RoutingAiClient(
        SimpleNamespace(),
        SimpleNamespace(),
        plain_backend="java",
        timeout_seconds=30,
        default_options=LlmRequestOptions(),
        mcp_services=(),
        laya_enabled=True,
    )
    assert client.route(AiRouteRequest(agent=True)).backend == "java"
    assert client.route(AiRouteRequest(chat_tools=("memory",))).capability == "legacy"


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
    java, plain, java_guard = AsyncMock(), AsyncMock(), AsyncMock()
    java.complete.return_value = "Java result"
    client = RoutingAiClient(
        java,
        plain,
        plain_backend="python",
        agent_backend="python",
        agent_service=agent,
        agent_code="helper",
        timeout_seconds=30,
        default_options=LlmRequestOptions(),
        mcp_services=(),
        laya_enabled=False,
    )
    guard = RoutedModelGuard(java_guard, seeded_model[0], plain_backend="python")
    chat = ChatService(
        client=client,
        model_guard=guard,
        model_router=ContentModelRouter(
            text_model="java-only-model",
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
        if mixed:
            assert answer == "Java result"
            java_guard.ensure_enabled.assert_awaited_once_with("java-only-model")
            assert agent.providers["openai-chat"].calls == []
        else:
            assert answer == "最终回答"
            java_guard.ensure_enabled.assert_not_called()
            java.complete.assert_not_called()
            assert agent.providers["openai-chat"].calls[0].resolved.model.alias == "local-model"
    finally:
        await chat.aclose()
        await client.aclose()
