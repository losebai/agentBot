"""纯 Python 入口装配与权限收紧，不启动真实机器人或数据库。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from tests.jobs.test_store import scope
from tests.jobs.test_tools import call, context, request, snapshot

from agent_bot.agent.contracts import ExecutionAuthority
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.runtime import ToolRuntime
from agent_bot.ai.routing import AiRouteRequest, select_route
from agent_bot.config import PluginConfig
from agent_bot.jobs.tools import JobTools


def config(**changes):
    """提供彼此不同的虚拟密钥，不加载本地环境文件。"""
    return PluginConfig(onebot_v11_access_token="o" * 32, chat_session_secret="s" * 32, **changes)


def test_job_routes_to_python_agent_and_configuration_needs_no_java():
    """Job 使用本地 Agent，授权依赖会话工具开关而非旧服务令牌。"""
    assert select_route(AiRouteRequest(chat_tools=("job",))).capability == "agent"
    assert config(chat_job_enabled=True, chat_mcp_enabled=True).chat_job_enabled
    with pytest.raises(ValidationError):
        config(chat_job_enabled=True)


def test_auto_speak_requires_registered_code_and_valid_targets():
    """开启自动发言必须给出完整纯 Python 配置及群上下文。"""
    values = dict(
        chat_auto_speak_enabled=True,
        chat_auto_speak_bot_id="10001",
        chat_auto_speak_groups={"10003"},
        chat_auto_speak_mcp_code="decision",
        chat_group_recent_context_enabled=True,
    )
    assert config(**values).chat_auto_speak_mcp_code == "decision"
    for field in (
        "chat_auto_speak_bot_id",
        "chat_auto_speak_groups",
        "chat_auto_speak_mcp_code",
        "chat_group_recent_context_enabled",
    ):
        with pytest.raises(ValidationError):
            config(**{key: value for key, value in values.items() if key != field})
    with pytest.raises(ValidationError):
        config(**values, chat_max_concurrent_requests=1)
    assert config(**values, chat_auto_speak_trigger_mode="message")


def test_llm_auto_speak_requires_own_model_without_laya_mcp():
    """LLM 模式仅依赖独立模型，不能误用普通聊天模型或强制绑定 Laya。"""
    values = dict(
        chat_auto_speak_enabled=True, chat_auto_speak_bot_id="10001",
        chat_auto_speak_groups={"10003"}, chat_group_recent_context_enabled=True,
        chat_auto_speak_decision_backend="llm", chat_auto_speak_decision_model="model:7",
        chat_auto_speak_llm_prompt="只判断本次是否适合发言",
    )
    selected = config(**values)
    assert selected.chat_auto_speak_decision_model == "model:7"
    assert selected.chat_auto_speak_mcp_code is None
    for model in ("", "\n", "bad\x00model"):
        with pytest.raises(ValidationError):
            config(**(values | {"chat_auto_speak_decision_model": model}))
    with pytest.raises(ValidationError):
        config(**(values | {"chat_auto_speak_decision_backend": "other"}))
    assert config().chat_auto_speak_decision_backend == "laya"


class EmptyConnector:
    """没有远端服务时只提供空工具集。"""

    @asynccontextmanager
    async def open(self, *args, **kwargs):
        """仍走生产 ToolRuntime 的绑定和筛选流程。"""
        yield SimpleNamespace(tools=(), warnings=())


async def test_runtime_revalidates_job_token_before_every_mutation(job_store):
    """工具已列出后令牌失效，不能凭缓存上下文创建任务。"""
    bridge = SimpleNamespace(
        list_tools=AsyncMock(return_value=()), context=AsyncMock(return_value=context())
    )
    runtime = ToolRuntime(EmptyConnector(), bridge, job_tools=JobTools(job_store))
    snap = snapshot()
    snap.mcps, snap.search_mcp = (), None
    authority = ExecutionAuthority(entry="qq", allowed_tools=("job",), chat_token="fake")
    async with runtime.open(snap, request(), authority) as bound:
        assert [tool.name for tool in bound.definitions] == ["job"]
        bridge.context.side_effect = AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        with pytest.raises(AgentError):
            await bound.call(call({"action": "create", "prompt": "提醒", "delaySeconds": 1}))
    assert await job_store.list_owned(scope()) == ()


async def test_qq_chat_preflight_reaches_python_agent_with_job_authority():
    """实际聊天编排不再导入旧后端选择器，并保留 Job 的请求授权。"""
    from agent_bot.ai.routing import RoutingAiClient
    from agent_bot.model_router import ContentModelRouter
    from agent_bot.models import LlmRequestOptions
    from agent_bot.services.chat import ChatService
    from agent_bot.state import ConversationContext

    agent = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(content="任务已创建")))
    client = RoutingAiClient(
        SimpleNamespace(),
        timeout_seconds=30,
        default_options=LlmRequestOptions(),
        mcp_services=(),
        agent_service=agent,
    )
    chat = ChatService(
        client=client,
        store=SimpleNamespace(context=lambda _: ConversationContext("", ())),
        system_prompt="可信提示",
        max_input_chars=2000,
        cooldown_seconds=0,
        max_concurrent_requests=2,
        model_guard=SimpleNamespace(ensure_enabled=AsyncMock()),
        model_router=ContentModelRouter(
            text_model="offline-model",
            vision_model="vision",
            image_model="image",
            audio_model="audio",
        ),
        web_search_enabled=False,
    )
    try:
        result = await chat.generate_queued(
            "session-1", "稍后提醒", request_id="r1", chat_tools=("job",), tool_session_token="fake"
        )
        assert result == "任务已创建"
        request_value, authority = agent.complete.await_args.args
        assert request_value.model == "offline-model"
        assert authority.entry == "qq" and authority.allowed_tools == ("job",)
        assert authority.chat_token.get_secret_value() == "fake"
    finally:
        await chat.aclose()
