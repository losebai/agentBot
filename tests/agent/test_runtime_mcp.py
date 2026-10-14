"""验证关联 MCP 的工具自动进入 Agent 和 LangChain，仍遵守请求授权。"""

from types import SimpleNamespace

import pytest

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials
from agent_bot.agent.tools.langchain import LangChainTools
from agent_bot.agent.tools.runtime import ToolRuntime

from .mcp_server import peer
from .test_mcp import server


@pytest.mark.parametrize("entry", ["qq", "admin"])
@pytest.mark.parametrize("with_agent", [True, False])
async def test_linked_mcp_tools_are_available_without_individual_agent_selection(
    entry: str, with_agent: bool,
) -> None:
    """预设未逐个勾选时仍绑定全部 21 个工具，并通过真实 LangChain 接口执行。"""
    names = ["Snapshot", "PowerShell", *[f"tool_{index}" for index in range(19)]]
    async with peer(tool_names=names) as (url, state):
        snapshot = SimpleNamespace(
            agent=SimpleNamespace(tools=("read_url",)) if with_agent else None,
            mcps=(server(url, allowed_tools=("*",)),), search_mcp=None,
        )
        request = AgentRequest(prompt="检查桌面", session_id="s", request_id="r")
        authority = ExecutionAuthority(
            entry=entry, allowed_tools=("read_url",), allowed_mcp_services=("docs",),
        )
        runtime = ToolRuntime(McpConnector(McpCredentials()))
        async with runtime.open(snapshot, request, authority) as bound:
            assert {tool.name for tool in bound.definitions} == {*names, "read_url"}
            tools = LangChainTools(bound)
            assert set(tools.by_name) == {*names, "read_url"}
            result = await tools.call(ToolCall(call_id="snapshot-1", name="Snapshot", arguments={}))
            assert result.content == "local result"
            assert result.call_id == "snapshot-1"
        assert [call["name"] for call in state["tool_requests"]] == ["Snapshot"]


@pytest.mark.parametrize("requested", [None, ("Snapshot",), ()])
async def test_mcp_whitelist_and_explicit_request_still_limit_available_tools(requested) -> None:
    """自动开放仅限服务白名单，请求可进一步缩小范围或明确禁用全部工具。"""
    async with peer(tool_names=["Snapshot", "PowerShell"]) as (url, state):
        snapshot = SimpleNamespace(
            agent=SimpleNamespace(tools=("read_url",)),
            mcps=(server(url, allowed_tools=("Snapshot",)),), search_mcp=None,
        )
        request = AgentRequest(prompt="检查桌面", session_id="s", request_id="r", tools=requested)
        authority = ExecutionAuthority(
            entry="qq", allowed_tools=("read_url",), allowed_mcp_services=("docs",),
        )
        async with ToolRuntime(McpConnector(McpCredentials())).open(
            snapshot, request, authority,
        ) as bound:
            expected = {"Snapshot", "read_url"} if requested is None else set(requested)
            assert {tool.name for tool in bound.definitions} == expected
            with pytest.raises(AgentError, match="AGENT_TOOL_UNAUTHORIZED"):
                await bound.call(ToolCall(call_id="denied", name="PowerShell", arguments={}))
        assert "tools/call" not in state["calls"]
        if requested == ():
            assert state["calls"] == []


async def test_unapproved_mcp_service_is_rejected_before_connecting() -> None:
    """自动开放不能绕过入口对 MCP 服务的授权，也不能提前连接未授权端点。"""
    async with peer() as (url, state):
        snapshot = SimpleNamespace(
            agent=SimpleNamespace(tools=()),
            mcps=(server(url, allowed_tools=("*",)),), search_mcp=None,
        )
        request = AgentRequest(prompt="检查桌面", session_id="s", request_id="r")
        with pytest.raises(AgentError, match="AGENT_TOOL_UNAUTHORIZED"):
            async with ToolRuntime(McpConnector(McpCredentials())).open(
                snapshot, request, ExecutionAuthority(entry="qq"),
            ):
                pytest.fail("未授权服务不能进入工具作用域")
        assert state["calls"] == []
