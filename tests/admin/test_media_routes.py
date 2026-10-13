"""已认证媒体上传、生成、记录和内容下载的真实 ASGI 测试。"""

import httpx
import pytest
from fastapi import FastAPI

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from agent_bot.media.contracts import MediaOwner
from media.conftest import media_database as media_database
from media.conftest import media_model as media_model
from media.test_generation_service import build
from media.test_inputs import picture

from .test_ai_routes import HEADERS, TOKEN


async def test_content_corruption_is_error_before_http_success(media_api):
    """损坏文件必须在发送成功响应头前拒绝，不能返回半个下载。"""
    api, _, runtime, _ = media_api
    asset = await runtime.media_store.put(
        owner=MediaOwner(entry="admin", session_id="admin_s"),
        purpose="input",
        data=picture(),
        mime_type="image/png",
    )
    (runtime.media_resources.config.directory / asset.id).write_bytes(b"corrupt")
    response = await api.get(f"/api/v1/media/{asset.id}/content", headers=HEADERS)
    assert response.status_code == 502
    assert response.json()["code"] == "MEDIA_FILE_CORRUPT"


async def test_large_valid_upload_and_expired_download(media_api):
    """有效图片超过普通 JSON 限额仍能上传，过期后明确返回 410。"""
    import io
    import random
    from datetime import timedelta

    from PIL import Image

    api, _, runtime, _ = media_api
    output = io.BytesIO()
    Image.frombytes("RGB", (256, 256), random.Random(42).randbytes(256 * 256 * 3)).save(
        output, format="PNG"
    )
    assert len(output.getvalue()) > 65536
    response = await api.post(
        "/api/v1/media/uploads?session_id=s",
        headers={**HEADERS, "Content-Type": "image/png"},
        content=output.getvalue(),
    )
    assert response.status_code == 201, response.text
    store = runtime.media_store
    original = store.clock
    store.clock = lambda: original() + timedelta(days=2)
    try:
        expired = await api.get(f"/api/v1/media/{response.json()['id']}/content", headers=HEADERS)
        assert expired.status_code == 410
    finally:
        store.clock = original


@pytest.fixture
async def media_api(
    admin_store, admin_runtime, media_database, media_model, agent_params, tmp_path
):
    """装配真实管理路由与媒体服务，只替换数据库及模型 HTTP 边界。"""
    service, resources, provider, _ = await build(
        media_model, media_database, agent_params, tmp_path
    )
    admin_runtime.media_service, admin_runtime.media_resources = service, resources
    admin_runtime.media_store = resources.store
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=TOKEN))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline"
    ) as api:
        try:
            yield api, app, admin_runtime, provider
        finally:
            await resources.aclose()


async def test_upload_generation_content_and_history(media_api):
    """内容端点必须认证，响应不含磁盘路径，QQ 附件不能从管理入口读取。"""
    api, _, runtime, _ = media_api
    uploaded = await api.post(
        "/api/v1/media/uploads?session_id=s",
        headers={**HEADERS, "Content-Type": "image/png"},
        content=picture(),
    )
    assert uploaded.status_code == 201, uploaded.text
    asset = uploaded.json()
    assert asset["purpose"] == "input" and "storage_key" not in asset
    url = f"/api/v1/media/{asset['id']}/content"
    assert (await api.get(url)).status_code == 401
    downloaded = await api.get(url, headers=HEADERS)
    assert downloaded.content == picture()
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.headers["cache-control"] == "no-store"
    generated = await api.post(
        "/api/v1/media/generations",
        headers=HEADERS,
        json={
            "model": "local-model",
            "session_id": "s",
            "prompt": "画猫",
            "output_type": "IMAGE",
            "image_format": "png",
        },
    )
    assert generated.status_code == 200, generated.text
    result = generated.json()
    assert result["attachments"][0]["status"] == "ready" and "response" not in result
    history = (await api.get("/api/v1/media/runs", headers=HEADERS)).json()
    assert history["items"][0]["execution_id"] == result["execution_id"]
    detail = (await api.get(f"/api/v1/media/runs/{result['execution_id']}", headers=HEADERS)).json()
    assert detail["attachments"] == result["attachments"]
    qq = await runtime.media_store.put(
        owner=MediaOwner(entry="qq", session_id="s"),
        purpose="output",
        data=picture(),
        mime_type="image/png",
    )
    assert (await api.get(f"/api/v1/media/{qq.id}", headers=HEADERS)).status_code == 404
    assert (await api.get("/api/v1/status", headers=HEADERS)).json()["python_media"][
        "storage_available"
    ]


@pytest.mark.parametrize(
    "extra",
    [
        {"endpoint": "http://bad"},
        {"owner": {"entry": "qq"}},
        {"max_bytes": 99999999},
        {"messages": [{"role": "system", "content": "forged"}]},
    ],
)
async def test_generation_dto_rejects_privileged_fields(media_api, extra):
    """外部请求不能扩大预算、伪造身份或注入系统历史。"""
    api, _, _, provider = media_api
    response = await api.post(
        "/api/v1/media/generations",
        headers=HEADERS,
        json={
            "model": "local-model",
            "session_id": "s",
            "prompt": "画猫",
            "output_type": "IMAGE",
            **extra,
        },
    )
    assert response.status_code == 422
    provider.generate.assert_not_called()


async def test_upload_actual_size_and_json_limits(media_api):
    """上传单独放宽为十兆，其他管理 JSON 保留六十四千字节限制。"""
    api, _, _, _ = media_api

    async def chunks():
        """无 Content-Length 分块输入，最后一字节越界。"""
        for _ in range(160):
            yield b"x" * 65536
        yield b"x"

    response = await api.post(
        "/api/v1/media/uploads?session_id=s", headers=HEADERS, content=chunks()
    )
    assert response.status_code == 413
    response = await api.post("/api/v1/media/generations", headers=HEADERS, content=b"x" * 65537)
    assert response.status_code == 413
