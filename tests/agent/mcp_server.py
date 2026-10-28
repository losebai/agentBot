"""真实回环 JSON-RPC/SSE 对端，仅替代测试中的外部 MCP 服务。"""

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


@asynccontextmanager
async def peer(*, foreign=False, repeating=False, status=200, call_wait=None,
               tool_name="lookup", tool_schema=None, tool_result=None, tool_names=None,
               response_sse=False, initialize_wait=None, session_id=False, delete_wait=None):
    """支持真实 SSE、初始化屏障和完整工具调用，验证并发与取消而不连接公网。"""
    queue = asyncio.Queue()
    state = {
        "calls": [], "active": 0, "entered": asyncio.Event(),
        "tool_requests": [], "authorization": [],
        "initializing": asyncio.Event(),
        "terminating": asyncio.Event(),
    }

    async def endpoint(request):
        """使用真实 HTTP 收发初始化、发现及调用消息，不连接公网。"""
        if request.method == "DELETE":
            state["terminating"].set()
            if delete_wait is not None:
                await delete_wait.wait()
            return Response(status_code=200)
        if status != 200:
            return Response(status_code=status)
        data = await request.json()
        state["calls"].append(data["method"])
        if "id" not in data:
            return Response(status_code=202)
        method = data["method"]
        if method == "initialize":
            state["initializing"].set()
            if initialize_wait is not None:
                await initialize_wait.wait()
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "offline", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": name,
                        "description": "test",
                        "inputSchema": tool_schema or {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    } for name in (tool_names if tool_names is not None else [tool_name])
                ]
            }
            if repeating:
                result["nextCursor"] = "again"
        elif method == "tools/call":
            state["tool_requests"].append(data["params"])
            state["authorization"].append(request.headers.get("Authorization"))
            state["entered"].set()
            if call_wait is not None:
                await call_wait.wait()
            result = tool_result if tool_result is not None else {
                "content": [{"type": "text", "text": "local result"}], "isError": False,
            }
        else:
            result = {}
        message = {"jsonrpc": "2.0", "id": data["id"], "result": result}
        if request.url.path == "/message":
            await queue.put(message)
            return Response(status_code=202)
        if response_sse:
            return Response(
                f"event: message\ndata: {json.dumps(message)}\n\n",
                media_type="text/event-stream",
            )
        return JSONResponse(message, headers={"mcp-session-id": "offline-session"}
                            if session_id else {})

    async def sse(request):
        async def events():
            state["active"] += 1
            try:
                destination = "http://foreign.invalid/message" if foreign else "/message"
                yield f"event: endpoint\ndata: {destination}\n\n"
                while True:
                    item = await queue.get()
                    yield f"event: message\ndata: {json.dumps(item)}\n\n"
            finally:
                state["active"] -= 1

        return StreamingResponse(events(), media_type="text/event-stream")

    app = Starlette(
        routes=[
            Route("/mcp", endpoint, methods=["POST", "DELETE"]),
            Route("/message", endpoint, methods=["POST"]),
            Route("/sse", sse),
        ]
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        assert server.started
        yield f"http://127.0.0.1:{sock.getsockname()[1]}", state
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        sock.close()
