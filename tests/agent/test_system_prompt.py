"""系统调用继承当前 Agent 人设，同时保留任务指令与原有权限边界。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentCreate, AgentUpdate
from agent_bot.ai.chat_errors import ChatAiRejectedError
from agent_bot.ai.routing import RoutingAiClient
from agent_bot.models import ChatMessage, LlmRequestOptions


@pytest.fixture
async def system_router(agent_database, agent_params, seeded_model):
    """读取真实 Agent 目录，只替换普通模型和 Agent 执行端口。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    agent = await catalog.create_agent(AgentCreate(
        name="当前助手", code="current", model_id=seeded_model[2].id,
        enabled=True, system_prompt="当前 Agent 的人设", tools=("memory",),
    ))
    plain = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(content="系统回复")))
    execution = SimpleNamespace(
        catalog=catalog, complete=AsyncMock(return_value=SimpleNamespace(content="图片总结")),
    )
    client = RoutingAiClient(
        plain, agent_service=execution, agent_code="current", timeout_seconds=30,
        default_options=LlmRequestOptions(), mcp_services=(), legacy_system_prompt="旧全局人设",
    )
    return client, plain, execution, catalog, agent


@pytest.mark.parametrize("role", ["新的 Agent 人设", ""])
async def test_system_text_reads_latest_agent_prompt_and_keeps_task(system_router, role):
    """人设更新或明确清空后立即生效；系统任务不重新启用 Agent 工具。"""
    client, plain, execution, catalog, agent = system_router
    await catalog.update_agent(agent.id, AgentUpdate(expected_revision=1, system_prompt=role))
    await client.complete(
        model="chat", session_id="s", prompt="参数无效", request_id="system-1",
        messages=(ChatMessage(role="system", content="旧全局人设"),
                  ChatMessage(role="system", content="只解释确定的校验事实"),
                  ChatMessage(role="user", content="参数无效")),
        use_agent=False, use_web_search=False,
    )
    request = plain.complete.await_args.args[0]
    assert request.model == "chat"
    prompts = [message.content for message in request.messages if message.role == "system"]
    assert prompts == ([role] if role else []) + ["只解释确定的校验事实"]
    execution.complete.assert_not_awaited()


async def test_system_vision_keeps_agent_prompt_without_granting_tools(system_router):
    """引用图片总结保留人设，仍使用独立视觉路由并禁止调用工具。"""
    client, plain, execution, _, _ = system_router
    await client.complete(
        model="vision", session_id="s", prompt="总结图片", request_id="summary-1",
        messages=(ChatMessage(role="system", content="只总结引用内容"),
                  ChatMessage(role="user", content="总结图片")),
        image_urls=("https://gchat.qpic.cn/image",), use_agent=False, use_web_search=False,
    )
    request, authority = execution.complete.await_args.args
    assert request.model == "vision" and request.agent_code is None
    assert request.tools == authority.allowed_tools == authority.allowed_mcp_services == ()
    assert [message.content for message in request.messages if message.role == "system"] == [
        "当前 Agent 的人设", "只总结引用内容",
    ]
    plain.complete.assert_not_awaited()


async def test_disabled_current_agent_does_not_fall_back_to_old_prompt(system_router):
    """已配置 Agent 被禁用时明确失败，不能继续使用过时的全局人设。"""
    client, plain, _, catalog, agent = system_router
    await catalog.update_agent(agent.id, AgentUpdate(expected_revision=1, enabled=False))
    with pytest.raises(ChatAiRejectedError, match="AGENT_CONFIG_DISABLED"):
        await client.complete(model="chat", session_id="s", prompt="校验", messages=(),
                              request_id="system-2", use_agent=False, use_web_search=False)
    plain.complete.assert_not_awaited()


@pytest.mark.parametrize("vision", [False, True])
async def test_system_tasks_share_composed_role_and_personality(system_router, vision):
    """普通通知与图片总结读取同一组合设定，任务指令及工具权限保持独立。"""
    client, plain, execution, catalog, agent = system_router
    await catalog.update_agent(agent.id, AgentUpdate(
        expected_revision=agent.revision, personality_prompt="温和简洁",
    ))
    await client.complete(
        model="vision" if vision else "chat", session_id="s", prompt="总结", request_id="persona",
        messages=(ChatMessage(role="system", content="只总结事实"),
                  ChatMessage(role="user", content="总结")),
        image_urls=("https://gchat.qpic.cn/image",) if vision else (),
        use_agent=False, use_web_search=False,
    )
    request = (execution if vision else plain).complete.await_args.args[0]
    assert [message.content for message in request.messages if message.role == "system"] == [
        "【角色设定】\n当前 Agent 的人设\n\n【人格设定】\n温和简洁", "只总结事实",
    ]
    if vision:
        assert request.tools == ()
    else:
        execution.complete.assert_not_awaited()
