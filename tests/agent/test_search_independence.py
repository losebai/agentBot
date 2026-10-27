"""独立搜索 MCP 保留原工具名和参数，不依赖内置搜索或全局绑定。"""

from types import SimpleNamespace

import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, McpCreate, ToolCall
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials
from agent_bot.agent.tools.runtime import ToolRuntime

from .mcp_server import peer
from .test_mcp import server


async def test_unlinked_search_services_are_not_implicitly_selected(
    agent_database, agent_params, seeded_model,
):
    """启用多个搜索类 MCP 不影响普通请求，也不会隐式连接或增加其配置版本。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    for code in ("first", "second"):
        await catalog.create_mcp(McpCreate(
            name=code, code=code, category="search", enabled=True,
            endpoint="http://localhost:9000/mcp", allowed_tools=("search",),
        ))
    snapshot = await catalog.snapshot(AgentRequest(
        model="local-model", prompt="你好", session_id="s", request_id="r", tools=(),
    ))
    assert snapshot.mcps == ()
    assert not hasattr(snapshot, "search_mcp")
    assert not any(key.startswith("mcp:") for key in snapshot.revisions)


@pytest.mark.parametrize("tool_name", ["search", "web_search"])
@pytest.mark.parametrize("entry", ["qq", "admin"])
async def test_search_mcp_uses_native_tool_without_builtin_authority(entry, tool_name):
    """搜索和同名工具都按远端 schema 执行，不要求任何本地搜索授权或特殊参数。"""
    schema = {
        "type": "object", "properties": {"keywords": {"type": "string"}},
        "required": ["keywords"], "additionalProperties": False,
    }
    async with peer(tool_name=tool_name, tool_schema=schema) as (url, state):
        remote = server(url, category="search", allowed_tools=(tool_name,))
        snapshot = SimpleNamespace(agent=None, mcps=(remote,))
        request = AgentRequest(prompt="搜索", session_id="s", request_id="r")
        authority = ExecutionAuthority(entry=entry, allowed_mcp_services=("docs",))
        async with ToolRuntime(McpConnector(McpCredentials())).open(
            snapshot, request, authority,
        ) as bound:
            assert [(tool.name, tool.source) for tool in bound.definitions] == [(tool_name, "mcp")]
            result = await bound.call(ToolCall(
                call_id="search-1", name=tool_name, arguments={"keywords": "原样查询"},
            ))
        assert result.content == "local result"
        assert state["tool_requests"] == [{
            "name": tool_name, "arguments": {"keywords": "原样查询"},
        }]
