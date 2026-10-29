"""认证管理API与真实临时表情库集成，不连接模型或QQ。"""

from io import BytesIO
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

from admin.conftest import admin_runtime as admin_runtime
from admin.conftest import admin_store as admin_store
from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from stickers.test_images import picture
from stickers.test_store import store as store

TOKEN = "offline-sticker-admin-token-longer-than-32-characters"
HEADERS = {"Authorization": "Bearer " + TOKEN}


@pytest.mark.parametrize("auto_analyze", [False, True, None])
async def test_upload_can_explicitly_skip_auto_analysis(api, monkeypatch, auto_analyze):
    """已配置模型时仍尊重单次上传开关，旧客户端省略参数保持原有自动识图行为。"""
    from unittest.mock import AsyncMock

    client, current, _ = api
    current.runtime.config = current.runtime.config.model_copy(update={
        "chat_sticker_model": "model:7",
    })

    async def analyze(id, *, expected_revision):
        """替代付费模型，仍实际写入识图结果供HTTP返回。"""
        return await current.store.set_analysis(
            id, expected_revision=expected_revision,
            description="已识别", emotion="开心", usage_hint="打招呼",
        )

    model = AsyncMock(side_effect=analyze)
    monkeypatch.setattr(current, "analyze", model)
    params = {"name": "测试图片"}
    if auto_analyze is not None:
        params["auto_analyze"] = str(auto_analyze).lower()
    response = await client.post("/api/v1/stickers/uploads", params=params,
                                 headers=HEADERS | {"Content-Type": "image/png"}, content=picture())
    assert response.status_code == 201
    if auto_analyze is False:
        assert response.json()["analysis_status"] == "pending"
        model.assert_not_awaited()
    else:
        assert response.json()["analysis_status"] == "ready"
        model.assert_awaited_once()


@pytest.fixture
async def api(store, admin_store, admin_runtime):
    """路由继承真实管理认证、错误封装和HTTP上传限额。"""
    from agent_bot.plugins.stickers.service import StickerService

    current = StickerService(store, admin_runtime)
    admin_runtime.plugins = SimpleNamespace(
        running_plugin=lambda name: (SimpleNamespace(service=current)
                                     if name == "stickers" else None),
    )
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=TOKEN))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline.test",
    ) as client:
        yield client, current, admin_runtime


async def test_sticker_upload_requires_management_authentication(api):
    """普通网页和QQ工具都不能绕过管理员上传图库。"""
    client, current, _ = api
    response = await client.post("/api/v1/stickers/uploads?name=笑", content=picture(),
                                 headers={"Content-Type": "image/png"})
    assert response.status_code == 401
    assert (await current.store.list())["total"] == 0


async def test_gif_upload_original_content_and_partial_revision_edit(api):
    """上传与标签更新保留GIF原件，旧修订不能覆盖新编辑。"""
    client, _, _ = api
    data = picture("GIF", animated=True)
    response = await client.post("/api/v1/stickers/uploads", params={
        "name": "猫猫挥手", "tags": '["招呼","可爱"]',
    }, headers=HEADERS | {"Content-Type": "image/gif"}, content=data)
    assert response.status_code == 201
    item = response.json()
    assert item["animated"] and item["tags"] == ["招呼", "可爱"]
    assert item["analysis_status"] == "pending"
    id = item["id"]
    original = await client.get(f"/api/v1/stickers/{id}/content", headers=HEADERS)
    assert original.content == data and original.headers["content-type"].startswith("image/gif")
    preview = await client.get(f"/api/v1/stickers/{id}/content?preview=true", headers=HEADERS)
    assert preview.content.startswith(b"\x89PNG")
    updated = await client.patch(f"/api/v1/stickers/{id}", headers=HEADERS, json={
        "expected_revision": 1, "tags": ["问候"],
    })
    assert updated.status_code == 200 and updated.json()["name"] == "猫猫挥手"
    assert updated.json()["tags"] == ["问候"] and updated.json()["revision"] == 2
    stale = await client.patch(f"/api/v1/stickers/{id}", headers=HEADERS,
                               json={"expected_revision": 1, "enabled": False})
    assert stale.status_code == 409 and stale.json()["code"] == "STICKER_CONFLICT"
    assert original.headers["cache-control"] == "no-store"


async def test_mislabeled_animated_webp_upload_returns_actual_content_type(api):
    """错误JPG后缀声明的WebP动图按真实类型入库、返回，预览保持静态。"""
    client, _, _ = api
    data = picture("WEBP", animated=True)
    response = await client.post("/api/v1/stickers/uploads", params={
        "name": "动图.jpg", "auto_analyze": "false",
    }, headers=HEADERS | {"Content-Type": "image/jpeg"}, content=data)
    assert response.status_code == 201
    item = response.json()
    assert item["mime_type"] == "image/webp" and item["animated"]
    assert item["frame_count"] == 2
    original = await client.get(f"/api/v1/stickers/{item['id']}/content", headers=HEADERS)
    assert original.content == data
    assert original.headers["content-type"].startswith("image/webp")
    assert original.headers["x-content-type-options"] == "nosniff"
    preview = await client.get(
        f"/api/v1/stickers/{item['id']}/content?preview=true", headers=HEADERS,
    )
    with Image.open(BytesIO(preview.content)) as image:
        assert image.format == "PNG" and not getattr(image, "is_animated", False)


async def test_large_binary_upload_uses_image_limit_instead_of_json_limit(api):
    """有效图片大于64KiB时仍按图片限额认证接收，不能被普通JSON中间件拒绝。"""
    import os

    client, _, _ = api
    buffer = BytesIO()
    Image.frombytes("RGB", (200, 200), os.urandom(120000)).save(buffer, format="PNG")
    data = buffer.getvalue()
    assert len(data) > 65536
    response = await client.post("/api/v1/stickers/uploads?name=静态图", headers=HEADERS | {
        "Content-Type": "image/png",
    }, content=data)
    assert response.status_code == 201


async def test_disabled_plugin_has_no_read_or_upload_capability(api):
    """已保存的启用草稿不等于插件已运行，HTTP不能隐式启动插件。"""
    client, _, runtime = api
    runtime.plugins.running_plugin = lambda _name: None
    response = await client.get("/api/v1/stickers", headers=HEADERS)
    assert response.status_code == 503 and response.json()["code"] == "STICKER_DISABLED"
    response = await client.post("/api/v1/stickers/uploads?name=图", headers=HEADERS | {
        "Content-Type": "image/png",
    }, content=picture())
    assert response.status_code == 503


async def test_context_search_disconnect_cancels_model_work(api):
    """浏览器离开页面触发ASGI断开时，模型任务须先取消收尾再归还请求租约。"""
    import asyncio
    import json

    client, current, _ = api
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow_search(*args, **kwargs):
        """只替换模型等待边界，不替换HTTP路由或断开处理。"""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    current.search = slow_search
    body = json.dumps({"context": "加班"}).encode()
    messages = []
    received = False

    async def receive():
        """先给出完整JSON正文，模型真正开始后才模拟连接断开。"""
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body, "more_body": False}
        await started.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        """保存真实ASGI状态以确认处理的是取消而不是普通超时。"""
        messages.append(message)

    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": "POST", "scheme": "http", "path": "/api/v1/stickers/search",
        "raw_path": b"/api/v1/stickers/search", "query_string": b"",
        "headers": [(b"authorization", ("Bearer " + TOKEN).encode()),
                    (b"content-type", b"application/json")],
        "client": ("127.0.0.1", 12345), "server": ("offline.test", 80),
    }
    await asyncio.wait_for(client._transport.app(scope, receive, send), 2)
    assert cancelled.is_set()
    assert next(m for m in messages if m["type"] == "http.response.start")["status"] == 499
