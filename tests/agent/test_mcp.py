import asyncio
import time
from datetime import datetime

import pytest

from agent_bot.agent.contracts import McpView, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials

from .mcp_server import peer


def server(url, transport="streamable", **patch):
    """构造离线服务快照，允许测试显式白名单或通配符发现。"""
    return McpView(
        id=1,
        revision=1,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        code="docs",
        name="docs",
        endpoint=url + ("/mcp" if transport == "streamable" else "/sse"),
        transport=transport,
        allowed_tools=patch.pop("allowed_tools", ("lookup",)),
        enabled=True,
        **patch,
    )


@pytest.mark.parametrize("count", [21, 32, 33])
async def test_discovery_accepts_windows_tool_count_with_bounded_limit(count: int) -> None:
    """完整保留已授权的 21 或 32 个工具，超过上限时明确拒绝而非截断。"""
    names = [f"tool_{index}" for index in range(count)]
    async with peer(tool_names=names) as (url, state):
        connector = McpConnector(McpCredentials())
        if count <= 32:
            async with connector.open(
                (server(url, allowed_tools=("*",)),), remaining=lambda: 5,
            ) as connection:
                assert [tool.name for tool in connection.tools] == names
        else:
            with pytest.raises(AgentError, match="AGENT_MCP_TOOL_LIMIT"):
                async with connector.open(
                    (server(url, allowed_tools=("*",)),), remaining=lambda: 5,
                ):
                    pass
        assert "tools/call" not in state["calls"]


@pytest.mark.parametrize("duplicate", [False, True])
async def test_multiple_servers_distinguish_tool_limit_and_name_conflict(duplicate: bool) -> None:
    """合并服务时分别检查总数量和重名，避免把数量超限误报为名称冲突。"""
    left = [f"left_{index}" for index in range(17)]
    right = left if duplicate else [f"right_{index}" for index in range(17)]
    async with peer(tool_names=left) as (first, _), peer(tool_names=right) as (second, _):
        code = "AGENT_TOOL_NAME_CONFLICT" if duplicate else "AGENT_MCP_TOOL_LIMIT"
        with pytest.raises(AgentError, match=code):
            async with McpConnector(McpCredentials()).open(
                (server(first, allowed_tools=("*",)), server(second, allowed_tools=("*",))),
                remaining=lambda: 5,
            ):
                pass


@pytest.mark.parametrize("transport", ["streamable", "sse"])
async def test_real_local_initialize_list_and_call(transport):
    async with peer() as (url, state):
        connector = McpConnector(McpCredentials())
        deadline = time.monotonic() + 5
        async with connector.open(
            (server(url, transport),), remaining=lambda: deadline - time.monotonic()
        ) as connection:
            assert [t.name for t in connection.tools] == ["lookup"]
            result = await connection.call(ToolCall(call_id="one", name="lookup", arguments={}))
            assert result.content == "local result"
        assert state["calls"].count("tools/call") == 1


async def test_sse_foreign_endpoint_never_receives_token():
    async with peer(foreign=True) as (url, state):
        connector = McpConnector(McpCredentials())
        with pytest.raises(AgentError):
            async with connector.open(
                (server(url, "sse", credential="fake-secret"),), remaining=lambda: 3
            ):
                pass
        assert state["calls"] == []


async def test_discovery_repeated_cursor_stops():
    async with peer(repeating=True) as (url, state):
        with pytest.raises(AgentError):
            async with McpConnector(McpCredentials()).open(
                (server(url),), remaining=lambda: 5
            ):
                pass
        assert state["calls"].count("tools/list") <= 2


async def test_authentication_failure_is_not_optional():
    async with peer(status=401) as (url, _state):
        with pytest.raises(AgentError) as failure:
            async with McpConnector(McpCredentials()).open(
                (server(url),), remaining=lambda: 5
            ):
                pass
        assert failure.value.code == "AGENT_MCP_AUTH_REJECTED"


async def test_optional_offline_warns_but_required_fails(unused_tcp_port):
    connector = McpConnector(McpCredentials())
    url = f"http://127.0.0.1:{unused_tcp_port}"
    async with connector.open((server(url),), remaining=lambda: 3) as connection:
        assert connection.tools == () and len(connection.warnings) == 1
    with pytest.raises(AgentError):
        async with connector.open((server(url, required=True),), remaining=lambda: 3):
            pass


@pytest.mark.parametrize("transport", ["streamable", "sse"])
async def test_cancelled_remote_call_joins_worker(transport):
    release = asyncio.Event()
    worker = None
    async with peer(call_wait=release) as (url, state):

        async def execute():
            nonlocal worker
            async with McpConnector(McpCredentials()).open(
                (server(url, transport),), remaining=lambda: 5
            ) as connection:
                worker = connection.workers[0]
                await connection.call(ToolCall(call_id="once", name="lookup", arguments={}))

        task = asyncio.create_task(execute())
        try:
            await asyncio.wait_for(state["entered"].wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 5)
            assert worker.task.done()
            assert state["calls"].count("tools/call") == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
