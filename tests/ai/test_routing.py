"""本地聊天入口复用模型目录、请求预算及错误分类。"""

import json
from types import SimpleNamespace

import httpx
import pytest

from agent_bot.ai.chat_errors import ChatAiRejectedError
from agent_bot.ai.routing import RoutedModelGuard, RoutingAiClient
from agent_bot.model_guard import ModelNotAvailableError
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import ChatMessage, LlmRequestOptions
from agent_bot.services.chat import ChatService
from agent_bot.services.replies import LlmReplyComposer, ReplyIntent, WorkflowStatusFacts
from agent_bot.state import ConversationStore

from .test_service import make_service


@pytest.mark.parametrize("status, expected", [
    ("started", "普通模型正文"), ("completed", "Agent 按人设生成的完成通知"),
])
async def test_image_completion_composer_routes_through_agent(status, expected) -> None:
    """真实 Composer、聊天服务与路由确保完成通知采用 Agent 正文，保持其他状态路由。"""

    class RecordingService:
        """替换模型执行边界，用不同正文识别实际选择的服务。"""

        def __init__(self, content: str) -> None:
            """保存模型正文和实际请求，避免触发供应商网络。"""

            self.content = content
            self.requests = []
            self.authorities = []

        async def complete(self, request, authority=None):
            """记录真实路由构造的请求并返回对应模型正文。"""

            self.requests.append(request)
            self.authorities.append(authority)
            return SimpleNamespace(content=self.content)

    ordinary = RecordingService("普通模型正文")
    agent = RecordingService("Agent 按人设生成的完成通知")
    router = RoutingAiClient(
        ordinary, agent_service=agent, agent_code="drawing-agent", timeout_seconds=2,
        default_options=LlmRequestOptions(), mcp_services=(),
    )
    store = ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60)
    chat = ChatService(
        client=router, store=store, system_prompt="你是群聊助手。", max_input_chars=100,
        cooldown_seconds=0, max_concurrent_requests=2,
        model_router=ContentModelRouter("text", "vision", "image", "audio"),
    )
    reply = await LlmReplyComposer(lambda: chat, timeout_seconds_provider=lambda: 2).compose(
        ReplyIntent(
            requestId="image-status", sessionId="image:task", code="WORKFLOW_STATUS",
            facts=WorkflowStatusFacts(status=status), originalPrompt="画一只猫",
        ),
    )

    assert reply is not None and reply.text == expected
    assert store.messages("image:task") == []
    if status == "completed":
        assert ordinary.requests == []
        assert len(agent.requests) == 1
        request = agent.requests[0]
        assert request.agent_code == "drawing-agent"
        assert any("图片已生成并已发送" in message.content for message in request.messages)
        assert agent.authorities[0].allowed_tools == ()
        assert agent.authorities[0].chat_token is None
    else:
        assert agent.requests == []


async def test_agent_preflight_checks_preset_model_instead_of_chat_alias() -> None:
    """预设 Agent 已绑定模型时，入队预检不能因普通聊天别名未登记而误拒绝。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    catalog = Mock(resolve=AsyncMock(side_effect=AssertionError("wrong model lookup")))
    agent_catalog = Mock(snapshot=AsyncMock(return_value=SimpleNamespace(resolved=object())))
    guard = RoutedModelGuard(catalog, agent_catalog=agent_catalog, agent_code="drawing-agent")

    await guard.ensure_enabled("unregistered-chat-alias", capability="agent")

    catalog.resolve.assert_not_awaited()
    request = agent_catalog.snapshot.await_args.args[0]
    assert request.agent_code == "drawing-agent" and request.model is None


async def test_disabled_agent_configuration_does_not_report_missing_model() -> None:
    """禁用预设属于 Agent 配置拒绝，不能进入模型缺失的清理和通知路径。"""
    from unittest.mock import AsyncMock, Mock

    from agent_bot.agent.errors import AgentError
    from agent_bot.model_guard import model_configuration_error_code

    agent_catalog = Mock(snapshot=AsyncMock(side_effect=AgentError(
        "AGENT_CONFIG_DISABLED", status_code=409,
    )))
    guard = RoutedModelGuard(Mock(), agent_catalog=agent_catalog, agent_code="disabled-agent")

    with pytest.raises(ChatAiRejectedError) as caught:
        await guard.ensure_enabled("valid-chat-alias", capability="agent")

    assert caught.value.error_code == "AGENT_CONFIG_DISABLED"
    assert model_configuration_error_code(caught.value) is None


async def test_default_text_resolves_local_model(catalog, ai_params, ai_database):
    """没有后端配置仍可按本地别名完成对话，缺失模型在发请求前拒绝。"""
    calls = []

    def upstream(req):
        """隔离供应商网络，同时记录实际上游模型名。"""
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": "本地回复"}, "done": True,
        })

    service = await make_service(catalog, ai_params, ai_database, upstream)
    router = RoutingAiClient(
        service, timeout_seconds=2, default_options=LlmRequestOptions(), mcp_services=()
    )
    try:
        args = dict(
            model="local", session_id="s", prompt="hello", request_id="r",
            messages=[ChatMessage(role="user", content="hello")],
            use_agent=False, use_web_search=False,
        )
        assert await router.complete(**args) == "本地回复"
        assert calls[0]["model"] == "exact/model"
        with pytest.raises(ChatAiRejectedError, match="AI_MODEL_NOT_FOUND"):
            await router.complete(**{**args, "model": "missing"})
        assert len(calls) == 1
    finally:
        await router.aclose()


async def test_model_guard_uses_local_catalog(catalog, ai_params, ai_database):
    """预检采用同一本地目录，不再读取旧服务的模型表。"""
    service = await make_service(catalog, ai_params, ai_database, lambda req: httpx.Response(200))
    try:
        guard = RoutedModelGuard(catalog)
        await guard.ensure_enabled("local")
        with pytest.raises(ModelNotAvailableError, match="AI_MODEL_NOT_FOUND"):
            await guard.ensure_enabled("missing")
    finally:
        await service.aclose()
