"""认证后的原始上传、直接生成、历史和有租约的媒体下载。"""

import asyncio

from fastapi import APIRouter, Query, Request
from starlette.requests import ClientDisconnect

from agent_bot.admin.media_models import MediaGenerationProbeRequest
from agent_bot.admin.media_requests import run_with_disconnect
from agent_bot.admin.streaming import ManagedStreamingResponse
from agent_bot.ai.contracts import Page
from agent_bot.database_pool import finish_database_write
from agent_bot.media.config import MAX_IMAGE_BYTES, UPLOAD_SECONDS
from agent_bot.media.contracts import (
    MediaAssetView,
    MediaGenerationResult,
    MediaOwner,
    MediaRunDetail,
    MediaRunSummary,
    MediaScope,
)
from agent_bot.media.errors import MediaError
from agent_bot.media.inputs import ImagePreparer
from agent_bot.media.store import finish_cleanup
from agent_bot.models import ChatMediaRequest, ChatMessage


class MediaContentResponse(ManagedStreamingResponse):
    """持有发送响应前已校验的文件，ASGI 结束时无条件归还租约。"""

    def __init__(self, iterator, *, lease, **kwargs):
        """文件租约不含运行时上下文，可安全交给响应生命周期管理。"""
        super().__init__(iterator, **kwargs)
        self.lease = lease

    async def __call__(self, scope, receive, send):
        """即使断开发生在第一个数据块之前，也等待文件关闭完成。"""
        try:
            await super().__call__(scope, receive, send)
        finally:
            await finish_cleanup(self.lease.__aexit__(None, None, None))


def install_media_routes(router: APIRouter, runtime) -> None:
    """路由继承管理 Bearer 验证，固定路径必须注册在媒体 ID 路径之前。"""

    def resources(*, generation=False):
        """状态查询不触发初始化；生成只允许显式选择 Python 后端。"""
        if generation and runtime.config.dispatch_chat_media_backend != "python":
            raise MediaError("MEDIA_REQUIRES_PYTHON", status_code=409)
        value = getattr(runtime, "media_resources", None)
        if value is None:
            raise MediaError("MEDIA_NOT_INITIALIZED", status_code=503)
        value.require_ready()
        return value

    @router.post("/media/uploads", response_model=MediaAssetView, status_code=201)
    async def upload(
        request: Request,
        session_id: str = Query(..., min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
    ):
        """不声明 Body 参数，使认证先于大文件读取；只保存通过实际解码的原图。"""
        async with runtime.lease():
            current = resources(generation=True)
            async with current.uploads.slot():

                async def receive_image():
                    """累计实际字节，不依赖可缺失或伪造的 Content-Length。"""
                    data = bytearray()
                    async for chunk in request.stream():
                        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                            raise MediaError("MEDIA_TOO_LARGE", status_code=413)
                        data.extend(chunk)
                    async def persist():
                        """正文读完后监听断开，解码和保存取消时等待线程收尾。"""
                        prepared = await ImagePreparer().prepare_async(
                            bytes(data), request.headers.get("content-type")
                        )
                        return await current.store.put(
                            owner=MediaOwner(entry="admin", session_id=f"admin_{session_id}"),
                            purpose="input",
                            data=prepared.data,
                            mime_type=prepared.media_type,
                        )

                    return await run_with_disconnect(request, persist())

                try:
                    return await asyncio.wait_for(receive_image(), UPLOAD_SECONDS)
                except asyncio.TimeoutError as error:
                    raise MediaError("MEDIA_UPLOAD_TIMEOUT", status_code=408) from error
                except ClientDisconnect as error:
                    raise MediaError("MEDIA_CANCELLED", status_code=499) from error

    @router.post("/media/generations", response_model=MediaGenerationResult)
    async def generate(body: MediaGenerationProbeRequest, request: Request):
        """同步请求的断开监听包住整个运行时租约。"""

        async def execute():
            """服务器补齐会话归属及配置预算，音频追加当前提问到对话。"""
            async with runtime.lease():
                resources(generation=True)
                config = runtime.config
                image = body.output_type == "IMAGE"
                session = f"admin_{body.session_id}"
                request_body = ChatMediaRequest(
                    model=body.model,
                    session_id=session,
                    request_id=request.state.request_id,
                    user="admin",
                    output_type=body.output_type,
                    prompt=body.prompt if image else None,
                    messages=()
                    if image
                    else (*body.messages, ChatMessage(role="user", content=body.prompt)),
                    timeout_seconds=max(
                        1,
                        int(
                            min(
                                120,
                                config.dispatch_chat_image_timeout_seconds
                                if image
                                else config.dispatch_chat_tts_timeout_seconds,
                            )
                        ),
                    ),
                    max_bytes=min(
                        MAX_IMAGE_BYTES,
                        config.dispatch_chat_image_max_bytes
                        if image
                        else config.dispatch_chat_tts_max_audio_bytes,
                    ),
                    image_format=(body.image_format or config.dispatch_chat_image_response_format)
                    if image
                    else None,
                    voice=None if image else (body.voice or config.dispatch_chat_tts_voice),
                    audio_format=None
                    if image
                    else (body.audio_format or config.dispatch_chat_tts_response_format),
                )
                return await runtime.media_service.generate(
                    request_body, MediaOwner(entry="admin", session_id=session)
                )

        return await run_with_disconnect(request, execute())

    @router.get("/media/runs", response_model=Page[MediaRunSummary])
    async def runs(
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = Query(None, max_length=1024),
        session_id: str | None = Query(None, max_length=256),
        status: str | None = Query(None, max_length=16),
    ):
        """仅列出管理入口记录，游标绑定入口与过滤条件。"""
        async with runtime.lease():
            return await resources().records.list_runs(
                limit=limit, cursor=cursor, session_id=session_id, status=status, entry="admin"
            )

    @router.get("/media/runs/{execution_id}", response_model=MediaRunDetail)
    async def run_detail(execution_id: str):
        """从可信运行记录恢复附件归属，不接受请求指定 owner。"""
        async with runtime.lease():
            current = resources()
            detail = await current.records.get_run(execution_id)
            if detail.entry != "admin":
                raise MediaError("MEDIA_RECORD_NOT_FOUND", status_code=404)
            assets = await current.store.list_execution(
                MediaScope(
                    owner=MediaOwner(entry="admin", session_id=detail.session_id),
                    execution_id=detail.execution_id,
                    execution_kind="media",
                )
            )
            return detail.model_copy(update={"attachments": assets})

    @router.get("/media/{id}", response_model=MediaAssetView)
    async def metadata(id: str):
        """仅允许已认证管理员查看管理端历史附件。"""
        async with runtime.lease():
            return await resources().store.describe(id, admin_only=True)

    @router.get("/media/{id}/content")
    async def content(id: str):
        """不在 URL 携带凭据，读取全程持有文件租约并禁用内容嗅探。"""
        async with runtime.lease():
            store = resources().store
            asset = await store.describe(id, admin_only=True)
            lease = store.open_content(id, admin_only=True)
            file = await lease.__aenter__()

        async def chunks():
            """流结束或断开后关闭文件，取消等待已开始的有界文件读取。"""
            while chunk := await finish_database_write(lambda: file.read(65536)):
                yield chunk

        return MediaContentResponse(
            chunks(),
            lease=lease,
            media_type=asset.mime_type,
            headers={
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{asset.id}"',
            },
        )
