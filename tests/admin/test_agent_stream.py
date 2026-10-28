import asyncio
import json
from contextlib import asynccontextmanager

from agent.test_engine import Tools

from .test_agent_routes import agent_api as agent_api
from .test_ai_routes import HEADERS


async def test_refresh_between_agent_preflight_and_stream_returns_json_error(agent_api):
    """预检后开始刷新时仍应在响应头之前返回 503，不能留下一个意外断开的 200 流。"""
    from agent_bot.core.lifecycle import RuntimeRefreshInProgressError

    api, _, runtime = agent_api
    entered = 0

    @asynccontextmanager
    async def lease():
        """第一次租约用于预检，第二次模拟刷新已关闭实际推理入口。"""
        nonlocal entered
        entered += 1
        if entered == 2:
            raise RuntimeRefreshInProgressError()
        yield

    runtime.lease = lease
    response = await api.post(
        "/api/v1/agent-runs/stream", headers=HEADERS,
        json={"agent_code": "helper", "prompt": "离线测试", "session_id": "refresh"},
    )
    assert response.status_code == 503
    assert response.json()["code"] == "RUNTIME_REFRESHING"


async def test_disconnect_after_tool_side_effect_does_not_retry(agent_api):
    _, app, runtime = agent_api
    leases = 0
    happened = asyncio.Event()

    class SlowTools(Tools):
        async def call(self, call):
            self.invoked.append(call.call_id)
            happened.set()
            await asyncio.Event().wait()

    tools = SlowTools()
    runtime.agent_service.tool_runtime = tools

    @asynccontextmanager
    async def lease():
        nonlocal leases
        leases += 1
        try:
            yield
        finally:
            leases -= 1

    runtime.lease = lease
    sent, delivered = [], False

    async def receive():
        nonlocal delivered
        if not delivered:
            delivered = True
            return {
                "type": "http.request",
                "body": json.dumps(
                    {"agent_code": "helper", "prompt": "hello", "session_id": "disconnect"}
                ).encode(),
            }
        await happened.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "method": "POST",
        "path": "/api/v1/agent-runs/stream",
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
    await asyncio.wait_for(app(scope, receive, send), 5)
    assert leases == tools.active == runtime.agent_service.active == 0
    assert tools.invoked == ["c1"]
    body = b"".join(m.get("body", b"") for m in sent)
    assert b"event: tool_start" in body and b"event: done" not in body
    run = (await runtime.agent_service.records.list_runs()).items[0]
    assert run.status == "cancelled"
    assert (await runtime.agent_service.records.list_steps(run.execution_id)).items[
        -1
    ].status == "unknown"
