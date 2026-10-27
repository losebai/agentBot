"""MCP 格式注解保持原样，不阻断工具发现，也不启用额外格式检查器。"""

from types import SimpleNamespace

import pytest

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall, ToolDefinition
from agent_bot.agent.errors import AgentError
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials
from agent_bot.agent.tools.langchain import LangChainTools
from agent_bot.agent.tools.runtime import ToolRuntime
from agent_bot.agent.tools.schema import ToolSchema

from .mcp_server import peer
from .test_mcp import server


async def test_mcp_uri_schema_survives_discovery_binding_and_native_call():
    """真实回环 MCP 的 URI 注解经过发现、授权和 LangChain 执行后仍完整保留。"""
    schema = {
        "type": "object", "properties": {"url": {"type": "string", "format": "uri"}},
        "required": ["url"], "additionalProperties": False,
        "$schema": "http://json-schema.org/draft-07/schema#",
    }
    async with peer(tool_name="fetchWebContent", tool_schema=schema) as (url, state):
        snapshot = SimpleNamespace(
            agent=None, mcps=(server(url, allowed_tools=("fetchWebContent",)),),
        )
        request = AgentRequest(prompt="读取网页", session_id="s", request_id="r")
        authority = ExecutionAuthority(entry="admin", allowed_mcp_services=("docs",))
        async with ToolRuntime(McpConnector(McpCredentials())).open(
            snapshot, request, authority,
        ) as bound:
            assert bound.definitions[0].input_schema == schema
            result = await LangChainTools(bound).call(ToolCall(
                call_id="fetch-1", name="fetchWebContent",
                arguments={"url": "https://example.com/article"},
            ))
        assert result.content == "local result"
        assert state["tool_requests"] == [{
            "name": "fetchWebContent", "arguments": {"url": "https://example.com/article"},
        }]


@pytest.mark.parametrize("format_name", ["uri", "date-time", "email", "vendor-format"])
def test_format_is_an_annotation_and_does_not_replace_type_validation(format_name):
    """遵循格式默认注解语义，原始格式和类型约束均保留，不注册额外检查器。"""
    schema = {"type": "object", "properties": {
        "value": {"type": "string", "format": format_name},
    }}
    tool = ToolDefinition(name="remote", source="mcp", input_schema=schema)
    assert ToolSchema.validate_arguments(tool, {"value": "server-defined-value"}) == {
        "value": "server-defined-value",
    }
    assert tool.input_schema["properties"]["value"]["format"] == format_name
    with pytest.raises(AgentError, match="AGENT_TOOL_ARGUMENTS_INVALID"):
        ToolSchema.validate_arguments(tool, {"value": 123})


def test_invalid_format_annotation_remains_a_schema_error():
    """格式注解本身须是字符串，兼容格式不能接受结构错误的 schema。"""
    tool = ToolDefinition(name="remote", source="mcp", input_schema={
        "type": "object", "properties": {"url": {"type": "string", "format": 123}},
    })
    with pytest.raises(AgentError, match="AGENT_TOOL_SCHEMA_INVALID"):
        ToolSchema.validate_definition(tool)
