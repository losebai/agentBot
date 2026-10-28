"""MCP 工作任务拥有 SDK 生命周期，取消时由同一任务完成资源释放。"""

import asyncio
import contextvars
import hashlib
import logging
import time
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field

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
from agent_bot.services.llm_timing import record_timing

logger = logging.getLogger(__name__)


def _error_leaves(error):
    """展开 SDK 异常组，供错误分类和只记录类型的诊断共用。"""
    leaves = []

    def visit(item):
        """保持原异常顺序，不读取可能含凭据或响应正文的异常字符串。"""
        nested = getattr(item, "exceptions", ())
        if nested:
            for child in nested:
                visit(child)
        else:
            leaves.append(item)

    visit(error)
    return leaves


def classify(error):
    """保留业务拒绝与临时连接错误，其余 SDK 失败统一归为协议错误。"""
    leaves = _error_leaves(error)
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
        reusable=False,
    ):
        """每个任务拥有 SDK 生命周期；只读管理目录不占用执行期工具预算。"""
        self.server, self.secret, self.remaining = server, secret, remaining
        self.result_converter = result_converter
        self.catalog = catalog
        self.reusable = reusable
        self.queue = asyncio.Queue(maxsize=1)
        self.ready = asyncio.get_running_loop().create_future()
        # 长连接只持有服务配置；不能继承上一请求的截止时间、身份或模型上下文。
        self.task = asyncio.create_task(self._serve(), context=contextvars.Context())
        self.tools = ()
        self.write_stream = None
        self.http_client = None
        self.failure = None

    async def _serve(self):
        """在同一任务进入和退出 SDK，转换结果时不丢弃决策的结构化契约。"""
        current = None
        stage = "connect"
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
                httpx2.Timeout(self.server.timeout_seconds, read=None) if self.reusable
                else min(self.server.timeout_seconds, self.remaining()),
                report_failure,
            )
            async with AsyncExitStack() as stack:
                if self.server.transport == "streamable":
                    client = await stack.enter_async_context(factory())
                    self.http_client = client
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
                            # 空闲事件流由运行时关闭；初始化与每次 RPC 仍各自有界。
                            sse_read_timeout=None if self.reusable else self.remaining(),
                            httpx_client_factory=factory,
                        )
                    )
                self.write_stream = streams[1]
                session = await stack.enter_async_context(
                    ClientSession(*streams[:2], read_timeout_seconds=self.server.timeout_seconds)
                )
                stage = "initialize"
                await asyncio.wait_for(session.initialize(), min(10, self.remaining()))
                stage = "discover"
                self.tools = await discover(
                    session, self.server, self.remaining, catalog=self.catalog,
                )
                self.ready.set_result(None)
                while True:
                    stage = "idle"
                    call, current = await self.queue.get()
                    stage = "call"
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
                    # 空闲连接只保留目录和认证快照，不长期引用上一请求的工具正文。
                    call = definition = result = value = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failure = self.failure or classify(error)
            logger.warning(
                "MCP 交互失败：service=%s stage=%s code=%s causes=%s",
                self.server.code, stage, failure.code,
                ",".join(dict.fromkeys(type(leaf).__name__ for leaf in _error_leaves(error))),
            )
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
        """先收紧退出请求预算，再取消并等待 SDK；不能遗留受屏蔽的会话 DELETE。"""
        if self.http_client is not None:
            # 长事件流的空闲 read=None 不适用于退出时的 DELETE/取消通知。
            # 清理 HTTP 最多一秒，为五秒的工作任务退出预算保留资源释放时间。
            self.http_client.timeout = httpx2.Timeout(1)
        # 先关发送流，避免 SDK 在已阻塞的 SSE POST 上等待礼貌取消通知。
        if self.write_stream is not None:
            await self.write_stream.aclose()
        self.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), 5)
        except asyncio.CancelledError:
            if not self.task.done():
                raise
        except asyncio.TimeoutError:
            # 强制归还套接字并再次取消后必须 join，移出连接池不能视为清理完成。
            try:
                if self.http_client is not None:
                    await self.http_client.aclose()
            finally:
                self.task.cancel()
                try:
                    await self.task
                except asyncio.CancelledError:
                    pass
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


@dataclass
class _BorrowedWorker:
    """独占借出的连接及本次截止时间，不在多个请求间共享执行队列。"""

    worker: McpWorker = field(init=False)
    key: tuple
    deadline: float
    busy: bool = True
    retired: bool = False
    reused: bool = False

    def remaining(self) -> float:
        """使用可重新绑定的单调时钟预算，不读取后台任务继承的请求上下文。"""
        return self.deadline - time.monotonic()


class McpConnector:
    """运行时可复用已授权连接；独占借用避免不同请求的工具调用互相排队。"""
    def __init__(self, credentials, *, reuse_connections: bool = False):
        """一次性调用默认自行关闭，长期宿主显式启用复用并负责最终释放。"""
        self.credentials = credentials
        self.reuse_connections = reuse_connections
        self._workers: list[_BorrowedWorker] = []
        self._closed = False

    async def _checkout(self, server, remaining, result_converter):
        """按完整服务快照和凭据隔离目录，版本变化只淘汰空闲的旧连接。"""
        if self._closed:
            raise AgentError("AGENT_MCP_UNAVAILABLE", status_code=503)
        seconds = remaining()
        if seconds <= 0:
            raise AgentError("AGENT_TIMEOUT", status_code=504)
        secret = self.credentials.resolve(server)
        snapshot = server.model_dump_json(exclude={"created_at", "updated_at"})
        fingerprint = hashlib.sha256(
            snapshot.encode() + b"\0"
            + (secret.get_secret_value().encode() if secret is not None else b""),
        ).digest()
        key = (server.id, server.revision, fingerprint, result_converter)
        obsolete = []
        borrowed = None
        for item in tuple(self._workers):
            if item.worker.server.id == server.id and item.key != key and (
                item.worker.server.revision <= server.revision
            ):
                item.retired = True
            healthy = not item.worker.task.done() and item.worker.failure is None
            if not item.busy and (item.retired or not healthy):
                self._workers.remove(item)
                obsolete.append(item.worker)
            elif (self.reuse_connections and borrowed is None and not item.busy
                  and item.key == key and healthy):
                borrowed = item
                item.busy = True
                item.reused = True
                item.deadline = time.monotonic() + seconds
        if borrowed is None:
            # 先登记借用，再等待网络准备；并发请求将建立独立连接而非等待此执行结束。
            borrowed = _BorrowedWorker(key, time.monotonic() + seconds)
            borrowed.worker = McpWorker(
                server, secret, borrowed.remaining, result_converter,
                reusable=self.reuse_connections,
            )
            self._workers.append(borrowed)
            new = True
        else:
            new = False
        try:
            with anyio.CancelScope(shield=True):
                results = await asyncio.gather(
                    *(worker.close() for worker in obsolete), return_exceptions=True,
                )
            if any(isinstance(result, BaseException) for result in results):
                raise AgentError("AGENT_MCP_CLOSE_FAILED", status_code=503)
            if new:
                await borrowed.worker.start()
            return borrowed
        except BaseException:
            self._workers.remove(borrowed)
            with anyio.CancelScope(shield=True):
                await borrowed.worker.close()
            raise

    async def _release(self, item: _BorrowedWorker, *, failed: bool) -> None:
        """成功连接最多保留一个空闲副本，失败和取消立即关闭且不重放工具。"""
        item.busy = False
        redundant = any(
            other is not item and not other.busy and other.key == item.key
            for other in self._workers
        )
        newer = any(
            other.worker.server.id == item.worker.server.id
            and other.worker.server.revision > item.worker.server.revision
            for other in self._workers
        )
        if (failed or self._closed or not self.reuse_connections or item.retired or newer
                or redundant or item.worker.task.done() or item.worker.failure is not None):
            if item in self._workers:
                self._workers.remove(item)
            await item.worker.close()

    async def aclose(self) -> None:
        """宿主排空请求后关闭全部缓存任务，避免热更新或退出遗留事件流。"""
        self._closed = True
        workers, self._workers = self._workers, []
        with anyio.CancelScope(shield=True):
            results = await asyncio.gather(
                *(item.worker.close() for item in workers), return_exceptions=True,
            )
        if any(isinstance(result, BaseException) for result in results):
            raise AgentError("AGENT_MCP_CLOSE_FAILED", status_code=503)

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
    async def open(
        self, servers, *, remaining, result_converter=convert_result, request_id: str | None = None,
    ):
        """独立服务并行准备，目录仍逐次校验权限；失败只沿用明确的可选服务规则。"""
        if len(servers) > 3:
            raise AgentError("AGENT_MCP_SERVICE_LIMIT", status_code=422)
        borrowed, warnings = [], []
        failed = False
        started = time.monotonic()
        prepared = False
        selected = [server for server in servers if server.allowed_tools]

        async def checkout(server):
            """准备一项即登记归还责任，其他服务准备时取消也不会漏掉已借出的连接。"""
            item = await self._checkout(server, remaining, result_converter)
            borrowed.append(item)
            return item

        try:
            disabled = {
                server.id: server.revision for server in servers if not server.allowed_tools
            }
            obsolete = []
            for item in tuple(self._workers):
                if item.worker.server.revision <= disabled.get(item.worker.server.id, -1):
                    item.retired = True
                    if not item.busy:
                        self._workers.remove(item)
                        obsolete.append(item.worker)
            with anyio.CancelScope(shield=True):
                closed = await asyncio.gather(
                    *(worker.close() for worker in obsolete), return_exceptions=True,
                )
            if any(isinstance(result, BaseException) for result in closed):
                raise AgentError("AGENT_MCP_CLOSE_FAILED", status_code=503)
            outcomes = await asyncio.gather(*(
                checkout(server) for server in selected
            ), return_exceptions=True)
            # 成功路径按配置顺序还原目录；取消路径则由上面的即时登记负责归还。
            borrowed = [value for value in outcomes if isinstance(value, _BorrowedWorker)]
            for server, outcome in zip(selected, outcomes, strict=True):
                if isinstance(outcome, BaseException):
                    if (not isinstance(outcome, AgentError) or remaining() <= 0
                            or server.required or outcome.code != "AGENT_MCP_UNAVAILABLE"):
                        raise outcome
                    warnings.append(ServiceWarning(code=outcome.code, service_code=server.code))
            connection = McpConnection([item.worker for item in borrowed], warnings)
            names = [t.name for t in connection.tools]
            if len(set(names)) != len(names):
                raise AgentError("AGENT_TOOL_NAME_CONFLICT", status_code=409)
            if len(names) > MAX_MCP_TOOLS:
                raise AgentError("AGENT_MCP_TOOL_LIMIT", status_code=422)
            prepared = True
            record_timing(
                "mcp_prepare", request_id, started, status="partial" if warnings else "ready",
                reused=bool(borrowed) and all(item.reused for item in borrowed),
            )
            yield connection
        except BaseException as error:
            failed = True
            if not prepared:
                record_timing(
                    "mcp_prepare", request_id, started,
                    status="cancelled" if isinstance(error, asyncio.CancelledError) else "failed",
                )
            raise
        finally:
            # 每个工作任务独占 SDK 生命周期；取消不遗留执行中的工具给后续请求。
            with anyio.CancelScope(shield=True):
                outcomes = await asyncio.gather(
                    *(self._release(item, failed=failed) for item in borrowed),
                    return_exceptions=True,
                )
            if not failed and any(isinstance(outcome, Exception) for outcome in outcomes):
                raise AgentError("AGENT_MCP_CLOSE_FAILED", status_code=503)
