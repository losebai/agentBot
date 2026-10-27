"""投稿视频上传复用媒体身份与实际 MP4 校验，不进入图片管线。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from fastapi import APIRouter, FastAPI
from starlette.responses import JSONResponse

from agent_bot.admin.media_routes import install_media_routes
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.errors import MediaError
from media.test_store import storage
from unit.test_comfy_video import MP4


async def test_video_upload_creates_owned_input_asset(
    media_database, agent_params, tmp_path, media_clock,
):
    """返回的 ID 可由本会话读取，非法容器不产生 ready 资产。"""
    @asynccontextmanager
    async def lease():
        """模拟宿主租约及上传并发槽。"""
        yield
    async with storage(media_database, agent_params, tmp_path, media_clock) as store:
        resources = SimpleNamespace(store=store, require_ready=lambda: None,
                                    uploads=SimpleNamespace(slot=lease))
        runtime = SimpleNamespace(media_resources=resources, lease=lease)
        app, router = FastAPI(), APIRouter(prefix='/api/v1')
        @app.exception_handler(MediaError)
        async def media_error(request, error):
            """独立路由测试仍使用生产错误类型的状态码。"""
            return JSONResponse({'code': error.code}, status_code=error.status_code)
        install_media_routes(router, runtime)
        app.include_router(router)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://offline.test') as client:
            response = await client.post('/api/v1/media/video/uploads?session_id=test',
                                         content=MP4, headers={'Content-Type': 'video/mp4'})
            assert response.status_code == 201, response.text
            asset = response.json()
            assert asset['mime_type'] == 'video/mp4' and asset['purpose'] == 'input'
            owner = MediaOwner(entry='admin', session_id='admin_test')
            assert (await store.describe(asset['id'], owner=owner)).size_bytes == len(MP4)
            bad = await client.post('/api/v1/media/video/uploads?session_id=test',
                                    content=b'not mp4', headers={'Content-Type': 'video/mp4'})
            assert bad.status_code == 422
