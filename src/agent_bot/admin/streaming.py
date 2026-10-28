"""先取得流的运行时租约再发送响应头，并在原任务内关闭迭代器。"""

import anyio
from starlette.responses import StreamingResponse


class ManagedStreamingResponse(StreamingResponse):
    """首帧前的租约拒绝可返回正常 HTTP 错误，连接取消仍等待同一任务完成收尾。"""

    async def stream_response(self, send) -> None:
        """预读首帧以进入生成器内的租约，避免已发 200 后才发现配置正在刷新。"""
        original = self.body_iterator
        try:
            empty = object()
            first = await anext(original, empty)
            if first is not empty:
                async def content():
                    """首帧只缓存一次，后续流仍逐帧转发，不移动生成器的任务所有权。"""
                    yield first
                    async for chunk in original:
                        yield chunk

                self.body_iterator = content()
            await super().stream_response(send)
        finally:
            # 断连会取消发送任务；必须在原上下文关闭，才能归还 ContextVar 和事务资源。
            with anyio.CancelScope(shield=True):
                try:
                    await self.body_iterator.aclose()
                finally:
                    if self.body_iterator is not original:
                        await original.aclose()
