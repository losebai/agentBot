"""角色与人格独立持久化，并在各类模型入口使用相同系统提示词。"""

import pytest
from pydantic import ValidationError

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentCreate, AgentRequest, AgentUpdate, ExecutionAuthority

from .test_service import service_for


@pytest.mark.parametrize("personality,expected", [
    ("", "  原有角色\n"),
    (" \n ", "  原有角色\n"),
    ("  简洁、耐心  ", "【角色设定】\n原有角色\n\n【人格设定】\n简洁、耐心"),
])
async def test_personality_is_independent_and_clearable(
    agent_database, agent_params, seeded_model, personality, expected,
):
    """各 Agent 的设定互不影响，清空人格时恢复原角色原文。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    first = await catalog.create_agent(AgentCreate(
        name="客服", code="support", enabled=True, model_id=seeded_model[2].id,
        system_prompt="  原有角色\n", personality_prompt=personality,
    ))
    second = await catalog.create_agent(AgentCreate(
        name="开发", code="developer", enabled=True, model_id=seeded_model[2].id,
        system_prompt="开发角色", personality_prompt="严谨",
    ))
    assert first.personality_prompt == personality
    assert await catalog.get_system_prompt(first.code) == expected
    updated = await catalog.update_agent(first.id, AgentUpdate(
        expected_revision=first.revision, personality_prompt="",
    ))
    assert updated.personality_prompt == ""
    assert await catalog.get_system_prompt(first.code) == "  原有角色\n"
    assert await catalog.get_system_prompt(second.code) == (
        "【角色设定】\n开发角色\n\n【人格设定】\n严谨"
    )


def test_combined_prompt_has_one_length_limit():
    """分别合法但组合超长的输入也必须拒绝，避免保存后执行失败。"""
    with pytest.raises(ValidationError, match="组合后的系统提示词不能超过 32000 字符"):
        AgentCreate(name="长提示", code="long", model_id=1,
                    system_prompt="角" * 20000, personality_prompt="人" * 20000)


async def test_agent_execution_uses_composed_prompt(agent_database, agent_params, seeded_model):
    """实际执行向模型发送组合设定，并保留运行时的权限约束。"""
    service = service_for(agent_database, agent_params)
    await service.catalog.create_agent(AgentCreate(
        name="客服", code="support", enabled=True, model_id=seeded_model[2].id,
        system_prompt="解答产品问题", personality_prompt="温和简洁", tools=("lookup",),
    ))
    await service.complete(
        AgentRequest(agent_code="support", prompt="你好", session_id="s", request_id="persona"),
        ExecutionAuthority(entry="admin", allowed_tools=("lookup",)),
    )
    messages = service.providers["openai-chat"].calls[0].messages
    system = "\n".join(message.text for message in messages if message.role == "system")
    assert "【角色设定】\n解答产品问题\n\n【人格设定】\n温和简洁" in system
