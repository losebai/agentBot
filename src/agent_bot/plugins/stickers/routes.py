"""表情库管理路由继承宿主认证，上传及模型调用均持有运行时租约。"""

import asyncio
import json

from fastapi import APIRouter, Query, Request
from pydantic import Field
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from agent_bot.admin.media_requests import run_with_disconnect
from agent_bot.agent.errors import AgentError
from agent_bot.ai.contracts import AiModel
from agent_bot.media.config import MAX_IMAGE_BYTES, UPLOAD_SECONDS
from agent_bot.plugins.stickers.contracts import StickerPatch, normalize_tags
from agent_bot.plugins.stickers.tool_contracts import StickerLibrarySearchArguments


class _Revision(AiModel):
    """识图提交也必须使用当前图库修订，避免覆盖管理员的新编辑。"""

    expected_revision: int = Field(ge=1, strict=True)


def install_sticker_routes(router: APIRouter, runtime) -> None:
    """路由仅借用运行插件，不因HTTP请求加载停用服务或任意文件。"""
    def service():
        """以真实运行状态为准，不能用数据库里待重启的启用选择授予能力。"""
        manager = getattr(runtime, "plugins", None)
        plugin = manager.running_plugin("stickers") if manager is not None else None
        if plugin is None or plugin.service is None:
            raise AgentError("STICKER_DISABLED", status_code=503)
        return plugin.service

    @router.get("/stickers")
    async def listing(query: str = Query("", max_length=200),
                      tag: str = Query("", max_length=32),
                      offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        """管理员可分页筛选停用条目，QQ工具仅使用已启用候选。"""
        async with runtime.lease():
            return await service().store.list(query=query, tag=tag, offset=offset, limit=limit)

    @router.post("/stickers/uploads", status_code=201)
    async def upload(request: Request, name: str = Query(..., min_length=1, max_length=255),
                     tags: str = Query("[]", max_length=4096), auto_analyze: bool = True):
        """认证后按实际字节计数，尊重本批识图开关并保留已经保存的原件。"""
        try:
            custom_tags = normalize_tags(json.loads(tags))
        except (ValueError, TypeError) as error:
            raise AgentError("STICKER_INVALID_INPUT", status_code=422) from error
        async with runtime.lease():
            current = service()
            async with current.uploads.slot():
                async def receive() -> bytes:
                    """声明长度不可信；动态容器还会在独立解码器中验证。"""
                    data = bytearray()
                    async for chunk in request.stream():
                        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                            raise AgentError("STICKER_TOO_LARGE", status_code=413)
                        data.extend(chunk)
                    return bytes(data)

                try:
                    data = await asyncio.wait_for(receive(), UPLOAD_SECONDS)
                    return await run_with_disconnect(request, current.upload(
                        name=name, data=data,
                        mime_type=request.headers.get("content-type", "application/octet-stream"),
                        tags=custom_tags, auto_analyze=auto_analyze,
                    ))
                except TimeoutError as error:
                    raise AgentError("MEDIA_UPLOAD_TIMEOUT", status_code=408) from error
                except ClientDisconnect as error:
                    raise AgentError("MEDIA_CANCELLED", status_code=499) from error

    @router.post("/stickers/search")
    async def search(body: StickerLibrarySearchArguments, request: Request):
        """管理页保留显式标签筛选，离开页面取消匹配后才归还运行时租约。"""
        async with runtime.lease():
            return await run_with_disconnect(request, service().search(
                body.context, tags=body.tags, limit=body.limit,
            ))

    @router.get("/stickers/{id}/content")
    async def content(id: str, preview: bool = False):
        """文件键只能由数据库确定，预览和动图原件都经过认证与完整性校验。"""
        async with runtime.lease():
            view, data = await service().store.read(id, preview=preview)
        return Response(data, media_type="image/png" if preview else view.mime_type,
                        headers={"X-Content-Type-Options": "nosniff"})

    @router.patch("/stickers/{id}")
    async def update(id: str, body: StickerPatch):
        """稀疏更新不回写未编辑值，空标签数组明确清除标签。"""
        async with runtime.lease():
            return await service().store.update(id, body)

    @router.post("/stickers/{id}/analyze")
    async def analyze(id: str, body: _Revision, request: Request):
        """重新识图为显式操作，连接取消时停止模型而不覆盖既有元数据。"""
        async with runtime.lease():
            return await run_with_disconnect(request, service().analyze(
                id, expected_revision=body.expected_revision,
            ))

    @router.delete("/stickers/{id}")
    async def delete(id: str, expected_revision: int = Query(..., ge=1)):
        """先撤销检索可见性再清理文件，旧修订不能误删已编辑表情。"""
        async with runtime.lease():
            await service().store.delete(id, expected_revision=expected_revision)
        return {"deleted": True}
