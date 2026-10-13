"""同步管理执行监听真实 ASGI 断开，取消后等待资源归还。"""

import asyncio

from agent_bot.media.errors import MediaError
from agent_bot.media.store import finish_cleanup


async def run_with_disconnect(request, operation):
    """JSON body 已解析后监听断开，执行和监听都在退出前显式回收。"""

    async def watch():
        """阻塞读取连接事件，不轮询，也不吞掉正文。"""
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return

    work, watcher = asyncio.create_task(operation), asyncio.create_task(watch())

    async def join():
        """收集两个任务的结果，包括取消时仍在收尾的文件和数据库工作。"""
        await asyncio.gather(work, watcher, return_exceptions=True)

    try:
        done, _ = await asyncio.wait((work, watcher), return_when=asyncio.FIRST_COMPLETED)
        if watcher in done:
            work.cancel()
            raise MediaError("MEDIA_CANCELLED", status_code=499)
        return await work
    finally:
        watcher.cancel()
        if not work.done():
            work.cancel()
        await finish_cleanup(join())
