import asyncio
import json
from contextlib import asynccontextmanager

from .test_ai_routes import HEADERS
from .test_ai_routes import python_api as python_api


async def test_chat_stream_asgi_disconnect_releases_lease(python_api):
    _, app, runtime = python_api
    leases = 0

    @asynccontextmanager
    async def lease():
        nonlocal leases
        leases += 1
        try:
            yield
        finally:
            leases -= 1

    runtime.lease = lease
    disconnect, messages = asyncio.Event(), []
    delivered = False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {
                "type": "http.request",
                "body": json.dumps(
                    {"prompt": "hi", "session_id": "disconnect", "messages": []}
                ).encode(),
            }
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        messages.append(message)
        if message["type"] == "http.response.body" and b"event: delta" in message.get("body", b""):
            disconnect.set()
            await asyncio.sleep(0)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "method": "POST",
        "path": "/api/v1/chat/stream",
        "query_string": b"",
        "headers": [
            (b"authorization", HEADERS["Authorization"].encode()),
            (b"content-type", b"application/json"),
        ],
        "scheme": "http",
        "server": ("offline", 80),
        "client": ("127.0.0.1", 1),
        "root_path": "",
    }
    await asyncio.wait_for(app(scope, receive, send), 3)
    assert leases == 0
    body = b"".join(message.get("body", b"") for message in messages)
    assert b"event: delta" in body and b"event: done" not in body
    records = await runtime.ai_service.sessions.list_records(
        session_id="admin_disconnect", status="cancelled"
    )
    assert len(records.items) == 1
