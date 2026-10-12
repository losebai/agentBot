"""使用 ASGI receive/send 验证认证前不读大 body、断开后取消并等待归还。"""

import asyncio
import json
from contextlib import asynccontextmanager

from .test_ai_routes import HEADERS
from .test_media_routes import media_api as media_api


async def test_slow_upload_times_out_and_releases_slot(media_api, monkeypatch):
    """无 Content-Length 且正文迟迟不结束时应用上传预算并归还槽。"""
    _, app, runtime, _ = media_api
    monkeypatch.setattr("agent_bot.admin.media_routes.UPLOAD_SECONDS", 0.02)
    sent = []

    async def receive():
        """模拟网络停顿，不提供伪造的正常结束消息。"""
        await asyncio.Event().wait()

    async def send(message):
        """记录服务器实际响应。"""
        sent.append(message)

    await asyncio.wait_for(
        app(scope("/api/v1/media/uploads", query=b"session_id=s"), receive, send), 2
    )
    assert sent[0]["status"] == 408
    assert runtime.media_resources.uploads.active == 0


async def test_third_upload_is_rejected_before_reading_body(media_api):
    """两个槽均占用时第三个上传不消耗网络正文。"""
    _, app, runtime, _ = media_api
    sent = []

    async def receive():
        """容量已耗尽时不应调用。"""
        raise AssertionError("不应读取第三个上传正文")

    async def send(message):
        """记录容量拒绝状态。"""
        sent.append(message)

    async with runtime.media_resources.uploads.slot(), runtime.media_resources.uploads.slot():
        await app(scope("/api/v1/media/uploads", query=b"session_id=s"), receive, send)
    assert sent[0]["status"] == 429
    assert runtime.media_resources.uploads.active == 0


def scope(path, *, authorized=True, query=b""):
    """构建可逐消息控制的真实 HTTP 请求。"""
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "method": "POST",
        "path": path,
        "query_string": query,
        "headers": [(b"content-type", b"application/json")]
        + ([(b"authorization", HEADERS["Authorization"].encode())] if authorized else []),
        "scheme": "http",
        "server": ("offline", 80),
        "client": ("127.0.0.1", 1),
        "root_path": "",
    }


async def test_upload_unauthorized_never_consumes_body(media_api):
    """攻击者即使宣称或发送大文件，认证失败也不触发一次 body 接收。"""
    _, app, _, _ = media_api
    reads, sent = 0, []

    async def receive():
        """任何一次接收都证明认证发生得过晚。"""
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"x" * 65537}

    async def send(message):
        """保存真实 ASGI 响应状态。"""
        sent.append(message)

    await app(
        scope("/api/v1/media/uploads", authorized=False, query=b"session_id=s"), receive, send
    )
    assert reads == 0
    assert (
        next(message["status"] for message in sent if message["type"] == "http.response.start")
        == 401
    )


async def test_sync_generation_disconnect_joins_runtime_lease(media_api):
    """HTTP 断开取消一次已开始的模型调用，历史标为取消并归还租约。"""
    _, app, runtime, provider = media_api
    started = asyncio.Event()
    leases = 0

    @asynccontextmanager
    async def lease():
        """断开后必须等待内部模型和数据库事务结束才归还。"""
        nonlocal leases
        leases += 1
        try:
            yield
        finally:
            leases -= 1

    async def generate(*args):
        """模拟已经发生上游提交但没有最终响应。"""
        started.set()
        await asyncio.Event().wait()

    runtime.lease = lease
    provider.generate.side_effect = generate
    sent, delivered = [], False

    async def receive():
        """先交付 JSON，等上游开始后发送真正的 http.disconnect。"""
        nonlocal delivered
        if not delivered:
            delivered = True
            return {
                "type": "http.request",
                "body": json.dumps(
                    {
                        "model": "local-model",
                        "session_id": "s",
                        "prompt": "猫",
                        "output_type": "IMAGE",
                    }
                ).encode(),
            }
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        """取消不得伪造成功响应。"""
        sent.append(message)

    await asyncio.wait_for(app(scope("/api/v1/media/generations"), receive, send), 5)
    assert leases == runtime.media_resources.generations.active == 0
    provider.generate.assert_awaited_once()
    assert (await runtime.media_resources.records.list_runs()).items[0].status == "cancelled"
    assert not any(message.get("status") == 200 for message in sent)


async def test_disconnect_after_generation_keeps_ready_asset(media_api):
    """文件已经发布但 HTTP 回复前断开时，记录详情仍能找到唯一产物。"""
    _, app, runtime, provider = media_api
    ready = asyncio.Event()
    original_put = runtime.media_store.put

    async def put(**kwargs):
        """暂停在文件 ready 与完成记录之间，取消不得重复调用供应商。"""
        await original_put(**kwargs)
        ready.set()
        await asyncio.Event().wait()

    runtime.media_store.put = put
    sent, delivered = [], False

    async def receive():
        """文件发布后发出真实客户端断开消息。"""
        nonlocal delivered
        if not delivered:
            delivered = True
            return {
                "type": "http.request",
                "body": json.dumps(
                    {
                        "model": "local-model",
                        "session_id": "s",
                        "prompt": "猫",
                        "output_type": "IMAGE",
                    }
                ).encode(),
            }
        await ready.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        """记录实际响应而不主动取消应用协程。"""
        sent.append(message)

    await asyncio.wait_for(app(scope("/api/v1/media/generations"), receive, send), 5)
    run = (await runtime.media_resources.records.list_runs()).items[0]
    from agent_bot.media.contracts import MediaOwner, MediaScope

    assets = await runtime.media_store.list_execution(
        MediaScope(
            owner=MediaOwner(entry="admin", session_id="admin_s"),
            execution_id=run.execution_id,
            execution_kind="media",
        )
    )
    assert run.status == "cancelled"
    assert len(assets) == 1 and assets[0].status == "ready"
    assert runtime.media_resources.generations.active == 0
    provider.generate.assert_awaited_once()
