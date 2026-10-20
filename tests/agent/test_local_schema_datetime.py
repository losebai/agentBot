"""本地记忆时间字段投影保留真实格式校验，不扩大远程工具schema能力。"""

import json

import pytest

from agent_bot.agent.contracts import ToolCall, ToolDefinition
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.langchain import LangChainTools
from agent_bot.agent.tools.local_media import project_local_schema
from agent_bot.agent.tools.runtime import BoundTools
from agent_bot.agent.tools.schema import ToolSchema
from agent_bot.services.tagged_memory import MemoryArguments


@pytest.fixture(scope="session", autouse=True)
def after_nonebot_init():
    """纯工具参数边界不启动QQ插件，不连接模型和业务数据库。"""


def memory_tools():
    """从真实记忆契约生成受控工具，原始参数仍由生产Pydantic校验。"""
    tool = ToolDefinition(name="memory", source="chat", input_schema=project_local_schema(
        "memory", MemoryArguments.model_json_schema(),
    ))
    return LangChainTools(BoundTools((tool,), {"memory": None}))


def memory_call(**times):
    """完整保存参数避免将时间格式失败与缺少业务字段混淆。"""
    return ToolCall(call_id="save", name="memory", arguments={
        "action": "save", "scope": "user", "key": "deploy", "content": "部署约定",
        "tags": ["部署"], **times,
    })


def test_registered_memory_datetime_schema_allows_valid_timezone_parameters():
    """新增时间注解不能阻断所有记忆工具调用，合法带时区日期应通过完整预检。"""
    assert memory_tools().validate_batch((memory_call(
        valid_from="2026-10-20T00:00:00+08:00", valid_until="2026-10-21T00:00:00Z",
        observed_at="2026-10-19T10:00:00Z",
    ),)) == {}


@pytest.mark.parametrize("field", ["valid_from", "valid_until", "observed_at"])
def test_projected_memory_still_rejects_invalid_datetime_without_raw_input(field):
    """投影不能移除原模型的格式验证，错误反馈仍定位字段且不回显用户输入。"""
    result = memory_tools().validate_batch((memory_call(**{field: "private-invalid-value"}),))
    feedback = json.loads(result["save"].content)["error"]
    assert feedback["code"] == "AGENT_TOOL_ARGUMENTS_INVALID"
    assert any(issue["field"] == field for issue in feedback["issues"])
    assert "private-invalid-value" not in result["save"].content


def test_projected_memory_still_rejects_naive_datetime():
    """字符串类型和日期格式通过后，既有业务时区边界仍必须执行。"""
    result = memory_tools().validate_batch((memory_call(valid_until="2026-10-21T00:00:00"),))
    feedback = json.loads(result["save"].content)["error"]
    assert any("timezone" in issue["message"] for issue in feedback["issues"])


def test_local_projection_does_not_allow_remote_datetime_formats():
    """远程工具不能借本地记忆兼容性获得未经批准的schema关键字。"""
    schema = {"type": "object", "properties": {
        "when": {"type": "string", "format": "date-time"},
    }}
    remote = ToolDefinition(name="remote-calendar", source="mcp", input_schema=project_local_schema(
        "remote-calendar", schema,
    ))
    with pytest.raises(AgentError, match="AGENT_TOOL_SCHEMA_UNSUPPORTED"):
        ToolSchema.validate_definition(remote)
