"""当前 QQ 队列的死信维护，不接收后端、命名空间或任务标识。"""

import asyncio

from fastapi import APIRouter
from starlette.responses import JSONResponse


def install_queue_routes(router: APIRouter, runtime) -> None:
    """复用管理认证与运行时租约，只清除所属文本和绘图队列的死信记录。"""

    @router.get("/queues/dead-letters")
    async def counts() -> dict[str, int | bool]:
        """排空期间仍可读失败数量，禁用或尚未启动的 QQ 队列明确不可用。"""
        async with runtime.lease(read_only=True):
            stores = getattr(runtime, "queue_stores", ())
            if not stores:
                return {"available": False, "text": 0, "images": 0, "total": 0}
            text, images = await asyncio.gather(*(store.dead_letter_length() for store in stores))
            return {"available": True, "text": text, "images": images, "total": text + images}

    @router.delete("/queues/dead-letters", response_model=None)
    async def clear() -> dict[str, int] | JSONResponse:
        """按各存储原子返回实际删除数；保留活动任务、图片、历史、记忆与回执。"""
        async with runtime.lease():
            stores = getattr(runtime, "queue_stores", ())
            if not stores:
                return JSONResponse(
                    {"code": "QUEUE_UNAVAILABLE", "message": "QQ 队列尚未运行，暂时无法清除死信。"},
                    status_code=503,
                )
            text = await stores[0].clear_dead_letters()
            images = await stores[1].clear_dead_letters()
            return {"cleared": text + images, "text": text, "images": images}
