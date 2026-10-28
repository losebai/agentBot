"""使用真实回环 MCP 验证连接复用、版本隔离、并发及资源回收。"""

import asyncio

import pytest
from pydantic import SecretStr

from agent_bot.agent.contracts import ToolCall
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials

from .mcp_server import peer
from .test_mcp import server


@pytest.mark.parametrize("transport", ["streamable", "sse"])
async def test_sequential_runs_reuse_discovery_and_reset_request_deadline(transport, caplog):
    """后续请求复用发现结果，但不能继承上一请求已耗尽的截止时间。"""
    async with peer() as (url, state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)
        saved = server(url, transport)
        previous_budget = [3]
        try:
            async with connector.open(
                (saved,), remaining=lambda: previous_budget[0], request_id="a" * 32,
            ) as first:
                original = first.workers[0]
                result = await first.call(ToolCall(call_id="first", name="lookup", arguments={}))
                assert result.content == "local result"
            assert not original.task.done()
            previous_budget[0] = 0
            async with connector.open(
                (saved,), remaining=lambda: 3, request_id="b" * 32,
            ) as second:
                assert second.workers[0] is original
                result = await second.call(ToolCall(call_id="second", name="lookup", arguments={}))
                assert result.content == "local result"
            assert state["calls"].count("initialize") == 1
            assert state["calls"].count("tools/list") == 1
            assert state["calls"].count("tools/call") == 2
            timings = [record.llm_timing for record in caplog.records
                       if hasattr(record, "llm_timing")]
            assert [item["reused"] for item in timings] == [False, True]
            assert [item["request_id"] for item in timings] == ["a" * 32, "b" * 32]
        finally:
            await connector.aclose()
        assert original.task.done()


async def test_concurrent_runs_do_not_queue_behind_another_requests_worker():
    """同服务的并发执行独占各自工作任务，不能因复用引入跨请求工具串行。"""
    async with peer() as (url, state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)
        saved = server(url)
        try:
            async with connector.open((saved,), remaining=lambda: 3) as first:
                async with connector.open((saved,), remaining=lambda: 3) as second:
                    assert first.workers[0] is not second.workers[0]
                    values = await asyncio.gather(*(
                        connection.call(ToolCall(call_id=str(i), name="lookup", arguments={}))
                        for i, connection in enumerate((first, second))
                    ))
                    assert [v.content for v in values] == ["local result", "local result"]
            assert state["calls"].count("initialize") == 2
        finally:
            await connector.aclose()


async def test_changed_credential_and_revision_retire_idle_connection():
    """配置版本或密钥变化重建连接，不能沿用旧认证及授权目录。"""
    async with peer() as (url, state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)
        saved = server(url, credential="offline-first")
        try:
            async with connector.open((saved,), remaining=lambda: 3) as first:
                original = first.workers[0]
                await first.call(ToolCall(call_id="first", name="lookup", arguments={}))
            changed = saved.model_copy(update={
                "revision": 2, "credential": SecretStr("offline-second"),
            })
            async with connector.open((changed,), remaining=lambda: 3) as second:
                assert second.workers[0] is not original
                await second.call(ToolCall(call_id="second", name="lookup", arguments={}))
            assert original.task.done()
            assert state["authorization"] == ["Bearer offline-first", "Bearer offline-second"]
        finally:
            await connector.aclose()


async def test_independent_servers_initialize_in_parallel():
    """两个初始化均到达屏障后才放行，串行连接会超时而不能通过测试。"""
    release = asyncio.Event()
    async with peer(tool_name="left", initialize_wait=release) as (left, left_state), peer(
        tool_name="right", initialize_wait=release,
    ) as (right, right_state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)

        async def prepare():
            """只测试连接和目录准备，不执行模型或远程副作用。"""
            servers = (
                server(left, allowed_tools=("left",)),
                server(right, allowed_tools=("right",)).model_copy(update={"id": 2}),
            )
            async with connector.open(servers, remaining=lambda: 3) as connection:
                assert {t.name for t in connection.tools} == {"left", "right"}

        task = asyncio.create_task(prepare())
        try:
            await asyncio.wait_for(asyncio.gather(
                left_state["initializing"].wait(), right_state["initializing"].wait(),
            ), timeout=1)
            release.set()
            await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await connector.aclose()


async def test_cancelled_borrower_discards_connection_without_replaying_tool():
    """取消正在执行的工具后淘汰连接，不能把未确认的操作留给下一个请求重放。"""
    release = asyncio.Event()
    async with peer(call_wait=release) as (url, state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)
        saved = server(url)
        workers = []

        async def invoke():
            """固化工作任务以验证取消后生命周期确实结束。"""
            async with connector.open((saved,), remaining=lambda: 3) as connection:
                workers.append(connection.workers[0])
                await connection.call(ToolCall(call_id="once", name="lookup", arguments={}))

        task = asyncio.create_task(invoke())
        try:
            await asyncio.wait_for(state["entered"].wait(), timeout=1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=3)
            assert workers[0].task.done()
            release.set()
            async with connector.open((saved,), remaining=lambda: 3) as connection:
                assert connection.workers[0] is not workers[0]
            assert state["calls"].count("tools/call") == 1
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await connector.aclose()


async def test_cancelled_parallel_prepare_releases_already_borrowed_connection():
    """一个服务已复用、另一个仍初始化时取消，已借出的连接也必须退出。"""
    release = asyncio.Event()
    async with peer(tool_name="left") as (left, _), peer(
        tool_name="right", initialize_wait=release,
    ) as (right, state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)
        first = server(left, allowed_tools=("left",))
        second = server(right, allowed_tools=("right",)).model_copy(update={"id": 2})
        try:
            async with connector.open((first,), remaining=lambda: 3) as connection:
                warm = connection.workers[0]

            async def prepare():
                """先复用已知健康连接，再等待独立服务的初始化。"""
                async with connector.open((first, second), remaining=lambda: 3):
                    pytest.fail("初始化屏障尚未解除")

            task = asyncio.create_task(prepare())
            try:
                await asyncio.wait_for(state["initializing"].wait(), timeout=1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=3)
                assert warm.task.done()
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            await connector.aclose()


async def test_disabling_all_tools_closes_idle_connection_without_discovery():
    """服务清空授权后立即归还旧空闲连接，不再连接远端或保留旧目录。"""
    async with peer() as (url, state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)
        saved = server(url)
        try:
            async with connector.open((saved,), remaining=lambda: 3) as connection:
                warm = connection.workers[0]
            disabled = saved.model_copy(update={"revision": 2, "allowed_tools": ()})
            async with connector.open((disabled,), remaining=lambda: 3) as connection:
                assert connection.tools == ()
            assert warm.task.done()
            assert state["calls"].count("initialize") == 1
        finally:
            await connector.aclose()


async def test_new_revision_does_not_close_connection_borrowed_by_inflight_request():
    """新版本不抢占正在执行的旧快照，旧请求归还后才关闭其连接。"""
    async with peer() as (url, state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)
        original = server(url, credential="offline-old")
        changed = original.model_copy(update={
            "revision": 2, "credential": SecretStr("offline-new"),
        })
        try:
            async with connector.open((original,), remaining=lambda: 3) as first:
                old = first.workers[0]
                async with connector.open((changed,), remaining=lambda: 3) as second:
                    current = second.workers[0]
                    await second.call(ToolCall(call_id="new", name="lookup", arguments={}))
                assert not old.task.done()
                await first.call(ToolCall(call_id="old", name="lookup", arguments={}))
            assert old.task.done() and not current.task.done()
            assert state["authorization"] == ["Bearer offline-new", "Bearer offline-old"]
        finally:
            await connector.aclose()


async def test_session_delete_timeout_does_not_leave_untracked_background_worker():
    """远端不响应会话 DELETE 时也必须完成退出，不能把后台任务移出池后遗留。"""
    release = asyncio.Event()
    async with peer(session_id=True, delete_wait=release) as (url, state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)
        async with connector.open((server(url),), remaining=lambda: 3) as connection:
            worker = connection.workers[0]
        closing = asyncio.create_task(connector.aclose())
        try:
            await asyncio.wait_for(state["terminating"].wait(), timeout=1)
            await asyncio.wait_for(asyncio.shield(closing), timeout=2)
            assert worker.task.done()
        finally:
            release.set()
            await asyncio.gather(closing, return_exceptions=True)
            await connector.aclose()
