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
    return McpView(
        id=1,
        revision=1,
        created_at=datetime.now(),
        updated_at=datetime.now(),
        code="docs",
        name="docs",
        endpoint=url + ("/mcp" if transport == "streamable" else "/sse"),
        transport=transport,
        allowed_tools=("lookup",),
        enabled=True,
        **patch,
    )


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
