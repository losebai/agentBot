"""Real loopback JSON-RPC/SSE peer for testing the installed MCP client."""

import asyncio
import json
import socket
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route


@asynccontextmanager
async def peer(*, foreign=False, repeating=False, status=200, call_wait=None):
    queue = asyncio.Queue()
    state = {"calls": [], "active": 0, "entered": asyncio.Event()}

    async def endpoint(request):
        if status != 200:
            return Response(status_code=status)
        data = await request.json()
        state["calls"].append(data["method"])
        if "id" not in data:
            return Response(status_code=202)
        method = data["method"]
        if method == "initialize":
            result = {
                "protocolVersion": "2025-03-26",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "offline", "version": "1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "lookup",
                        "description": "test",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                    }
                ]
            }
            if repeating:
                result["nextCursor"] = "again"
        elif method == "tools/call":
            state["entered"].set()
            if call_wait is not None:
                await call_wait.wait()
            result = {"content": [{"type": "text", "text": "local result"}], "isError": False}
        else:
            result = {}
        message = {"jsonrpc": "2.0", "id": data["id"], "result": result}
        if request.url.path == "/message":
            await queue.put(message)
            return Response(status_code=202)
        return JSONResponse(message)

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
            Route("/mcp", endpoint, methods=["POST"]),
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
