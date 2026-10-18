"""普通文件下载使用离线 HTTP 传输验证字节限额、媒体类型及重定向边界。"""

import httpx
import pytest

from agent_bot.materials.receiver import FileDownloader
from agent_bot.media.errors import MediaError


async def test_download_returns_original_bytes_and_normalized_mime():
    """原件字节保持不变，Content-Type 的参数不进入素材类型。"""
    downloader = FileDownloader(httpx.MockTransport(lambda request: httpx.Response(
        200, content=b"document", headers={"Content-Type": "application/pdf; charset=utf-8"},
    )))
    assert await downloader.fetch("https://files.qq.com/file") == (b"document", "application/pdf")


async def test_download_rejects_private_redirect_before_request():
    """可信文件地址的重定向不能扩大为访问本机或内网。"""
    requests = []

    def redirect(request):
        """记录真正进入 HTTP 层的请求，禁止私网跳转发起连接。"""
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    with pytest.raises(MediaError, match="MATERIAL_DOWNLOAD_FAILED"):
        await FileDownloader(httpx.MockTransport(redirect)).fetch("https://files.qq.com/file")
    assert requests == ["https://files.qq.com/file"]


async def test_stream_without_content_length_is_still_bounded():
    """缺少声明长度的流也受实际二十 MiB 限额约束。"""
    class Oversized(httpx.AsyncByteStream):
        """逐块构造超限响应，避免测试分配整个大文件。"""

        async def __aiter__(self):
            """第二十一块越界，应在完整接收前拒绝。"""
            for _ in range(21):
                yield b"x" * (1024 * 1024)

    downloader = FileDownloader(httpx.MockTransport(lambda request: httpx.Response(
        200, stream=Oversized(),
    )))
    with pytest.raises(MediaError, match="MEDIA_TOO_LARGE"):
        await downloader.fetch("https://files.qq.com/file")
