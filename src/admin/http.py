"""管理请求正文限额和脱敏关联标识，上传入口认证后才读取大文件。"""

import logging
import uuid

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_bot.admin.models import ApiError
from agent_bot.media.errors import MediaError

logger = logging.getLogger(__name__)


class ManagementMiddleware:
    """管理请求限制大小；准确匹配上传入口时在鉴权后流式计数。"""

    def __init__(self, app: ASGIApp) -> None:
        """Wrap the application without altering non-management requests."""
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """普通管理正文上限六十四千字节，仅精确匹配的上传允许十兆流式输入。"""
        if scope["type"] != "http" or not scope["path"].startswith("/api/"):
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        response_started = False

        async def add_headers(message: Message) -> None:
            """Keep sensitive responses uncached and correlate failures without input echo."""
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                message["headers"] = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() not in {b"cache-control", b"x-request-id"}
                ] + [(b"cache-control", b"no-store"), (b"x-request-id", request_id.encode())]
            await send(message)

        is_upload = scope["method"] == "POST" and scope["path"] == "/api/v1/media/uploads"
        if is_upload:
            stream_size = 0

            async def replay() -> Message:
                """认证后才读取上传正文，按实际分块累计硬上限。"""
                nonlocal stream_size
                message = await receive()
                stream_size += len(message.get("body", b""))
                if stream_size > 10 * 1024 * 1024:
                    raise MediaError("MEDIA_TOO_LARGE", status_code=413)
                return message
        else:
            chunks, size = [], 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > 65536:
                    error = ApiError(
                        code="REQUEST_TOO_LARGE", message="请求超过大小限制", request_id=request_id
                    )
                    await JSONResponse(error.model_dump(), status_code=413)(
                        scope, receive, add_headers
                    )
                    return
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            delivered = False

            async def replay() -> Message:
                """Replay the bounded body once, then preserve disconnect/cancellation signals."""
                nonlocal delivered
                if delivered:
                    return await receive()
                delivered = True
                return {"type": "http.request", "body": b"".join(chunks), "more_body": False}

        try:
            await self.app(scope, replay, add_headers)
        except Exception as error:
            if response_started:
                raise
            logger.warning("Management request failed: %s", type(error).__name__)
            body = ApiError(
                code="INTERNAL_ERROR", message="服务暂时无法完成请求", request_id=request_id
            )
            await JSONResponse(body.model_dump(), status_code=500)(scope, receive, add_headers)
