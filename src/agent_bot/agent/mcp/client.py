"""MCP 工作任务拥有 SDK 生命周期，取消时由同一任务完成资源释放。"""

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
import httpx2
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client

from agent_bot.agent.contracts import MAX_MCP_TOOLS, ServiceWarning
from agent_bot.agent.errors import AgentError
from agent_bot.agent.mcp.discovery import convert_result, discover
from agent_bot.agent.mcp.http import MAX_MCP_RESPONSE_BYTES, client_factory
from agent_bot.agent.tools.schema import ToolSchema


def classify(error):
    leaves = []

    def visit(item):
        nested = getattr(item, "exceptions", ())
        if nested:
            for child in nested:
                visit(child)
        else:
            leaves.append(item)

    visit(error)
    for leaf in leaves:
        if isinstance(leaf, AgentError):
            return leaf
    if leaves and all(
        isinstance(e, (httpx2.ConnectError, httpx2.TimeoutException, asyncio.TimeoutError))
        for e in leaves
    ):
        return AgentError("AGENT_MCP_UNAVAILABLE", status_code=503)
    return AgentError("AGENT_MCP_PROTOCOL_ERROR", status_code=502)


class McpWorker:
    """持有单个 MCP 服务连接和显式结果转换器。"""
    def __init__(
        self, server, secret, remaining, result_converter=convert_result, *, catalog=False,
    ):
        """每个任务拥有 SDK 生命周期；只读管理目录不占用执行期工具预算。"""
        self.server, self.secret, self.remaining = server, secret, remaining
        self.result_converter = result_converter
        self.catalog = catalog
        self.queue = asyncio.Queue(maxsize=1)
        self.ready = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self._serve())
        self.tools = ()
        self.write_stream = None
        self.failure = None

    async def _serve(self):
        """在同一任务进入和退出 SDK，转换结果时不丢弃决策的结构化契约。"""
        current = None
        try:

            def report_failure(error):
                """保留底层限额错误，避免 SDK 将响应中断重新包装成通用协议错误。"""
                self.failure = error
                if not self.ready.done():
                    self.ready.set_result(error)
                if current is not None and not current.done():
                    current.set_result(error)

            factory = client_factory(
                self.server,
                self.secret,
                min(self.server.timeout_seconds, self.remaining()),
                report_failure,
            )
            async with AsyncExitStack() as stack:
                if self.server.transport == "streamable":
                    client = await stack.enter_async_context(factory())
                    streams = await stack.enter_async_context(
                        streamable_http_client(
                            self.server.endpoint, http_client=client,
                            max_sse_event_size=MAX_MCP_RESPONSE_BYTES,
                        )
                    )
                else:
                    streams = await stack.enter_async_context(
                        sse_client(
                            self.server.endpoint,
                            timeout=min(10, self.remaining()),
                            sse_read_timeout=self.remaining(),
                            httpx_client_factory=factory,
                        )
                    )
                self.write_stream = streams[1]
                session = await stack.enter_async_context(
                    ClientSession(*streams[:2], read_timeout_seconds=self.server.timeout_seconds)
                )
                await asyncio.wait_for(session.initialize(), min(10, self.remaining()))
                self.tools = await discover(
                    session, self.server, self.remaining, catalog=self.catalog,
                )
                self.ready.set_result(None)
                while True:
                    call, current = await self.queue.get()
                    definition = next((t for t in self.tools if t.name == call.name), None)
                    if definition is None:
                        raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
                    ToolSchema.validate_arguments(definition, call.arguments)
                    result = await session.call_tool(
                        call.name,
                        call.arguments,
                        read_timeout_seconds=min(self.server.timeout_seconds, self.remaining()),
                    )
                    value = self.result_converter(call, result)
                    if not current.done():
                        current.set_result(value)
                    current = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = self.failure or classify(error)
            if not self.ready.done():
                self.ready.set_result(failure)
            if current is not None and not current.done():
                current.set_result(failure)
        finally:
            failure = AgentError("AGENT_MCP_UNAVAILABLE", status_code=503)
            if not self.ready.done():
                self.ready.set_result(failure)
            if current is not None and not current.done():
                current.set_result(failure)

    async def start(self):
        try:
            result = await asyncio.wait_for(asyncio.shield(self.ready), min(10, self.remaining()))
        except asyncio.TimeoutError as error:
            raise AgentError("AGENT_MCP_UNAVAILABLE", status_code=503) from error
        if isinstance(result, AgentError):
            raise result

    async def call(self, call):
        if self.task.done():
            raise AgentError("AGENT_MCP_UNAVAILABLE", status_code=503)
        future = asyncio.get_running_loop().create_future()
        await self.queue.put((call, future))
        result = await asyncio.wait_for(future, min(self.server.timeout_seconds, self.remaining()))
        if isinstance(result, AgentError):
            raise result
        return result

    async def close(self):
        # Closing the public send stream prevents the SDK's shielded courtesy
        # cancellation notification from waiting on an already blocked SSE POST.
        if self.write_stream is not None:
            await self.write_stream.aclose()
        self.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 5)
        except asyncio.CancelledError:
            if not self.task.done():
                raise
        except asyncio.TimeoutError:
            raise AgentError("AGENT_MCP_CLOSE_TIMEOUT", status_code=503) from None


class McpConnection:
    def __init__(self, workers, warnings):
        self.workers, self.warnings = tuple(workers), tuple(warnings)
        self.tools = tuple(t for w in workers for t in w.tools)

    async def call(self, call):
        for worker in self.workers:
            if any(tool.name == call.name for tool in worker.tools):
                return await worker.call(call)
        raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)


class McpConnector:
    """打开经过目录授权的 MCP 服务，并统一管理退出与取消。"""
    def __init__(self, credentials):
        self.credentials = credentials

    async def list_tools(self, server, *, remaining):
        """只读发现管理目录，不向调用方提供可执行连接，取消时等待所属任务退出。"""
        worker = McpWorker(server, self.credentials.resolve(server), remaining, catalog=True)
        try:
            await worker.start()
            return worker.tools
        finally:
            with anyio.CancelScope(shield=True):
                await worker.close()

    @asynccontextmanager
    async def open(self, servers, *, remaining, result_converter=convert_result):
        """限定合并工具数量并单独拒绝重名，固定决策仍使用严格结构化解析。"""
        if len(servers) > 3:
            raise AgentError("AGENT_MCP_SERVICE_LIMIT", status_code=422)
        workers, warnings = [], []
        failed = False
        try:
            for server in servers:
                # 管理端关闭全部工具后保留服务配置，但执行不再建立无用连接。
                if not server.allowed_tools:
                    continue
                if remaining() <= 0:
                    raise AgentError("AGENT_TIMEOUT", status_code=504)
                worker = McpWorker(
                    server, self.credentials.resolve(server),
                    remaining, result_converter,
                )
                workers.append(worker)
                try:
                    await worker.start()
                except AgentError as error:
                    await worker.close()
                    workers.remove(worker)
                    if remaining() <= 0 or server.required or error.code != "AGENT_MCP_UNAVAILABLE":
                        raise
                    warnings.append(ServiceWarning(code=error.code, service_code=server.code))
            connection = McpConnection(workers, warnings)
            names = [t.name for t in connection.tools]
            if len(set(names)) != len(names):
                raise AgentError("AGENT_TOOL_NAME_CONFLICT", status_code=409)
            if len(names) > MAX_MCP_TOOLS:
                raise AgentError("AGENT_MCP_TOOL_LIMIT", status_code=422)
            yield connection
        except BaseException:
            failed = True
            raise
        finally:
            # Each worker owns SDK enter/exit; closing it from a stream task is safe.
            with anyio.CancelScope(shield=True):
                outcomes = await asyncio.gather(
                    *(w.close() for w in workers), return_exceptions=True
                )
            if not failed and any(isinstance(outcome, Exception) for outcome in outcomes):
                raise AgentError("AGENT_MCP_CLOSE_FAILED", status_code=503)
