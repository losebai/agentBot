"""可信 QQ 下载必须保留公网连接校验和重定向边界。"""

import httpx
import pytest

from agent_bot.media.errors import MediaError
from media.test_inputs import picture


async def test_qq_redirect_download_and_domain_revalidation():
    """可信重定向成功，任一非可信跳转在发请求前拒绝。"""
    from agent_bot.media.download import QQImageDownloader

    calls = []

    def handle(request):
        """模拟有签名参数的 CDN，不向日志保存地址。"""
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(302, headers={"location": "https://qpic.cn/final"})
        return httpx.Response(200, content=picture(), headers={"content-type": "image/png"})

    downloader = QQImageDownloader(transport=httpx.MockTransport(handle))
    assert (
        await downloader.fetch("https://gchat.qpic.cn/input?key=private")
    ).media_type == "image/png"
    assert len(calls) == 2
    calls.clear()
    for url in [
        "http://qpic.cn/input",
        "https://qpic.cn.evil.invalid/input",
        "https://127.0.0.1/a",
    ]:
        with pytest.raises(MediaError, match="MEDIA_IMAGE_URL_REJECTED"):
            await downloader.fetch(url)
    assert calls == []
    bad = QQImageDownloader(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(302, headers={"location": "https://other.invalid/a"})
        )
    )
    with pytest.raises(MediaError, match="MEDIA_IMAGE_URL_REJECTED"):
        await bad.fetch("https://qpic.cn/a")


async def test_download_limits_and_default_numeric_transport(monkeypatch):
    """实际字节和重定向次数均有上限，生产默认固定数字地址连接。"""
    from agent_bot.agent.tools.public_http import PublicTransport
    from agent_bot.media import download

    assert isinstance(download.QQImageDownloader().make_transport(), PublicTransport)
    monkeypatch.setattr(download, "MAX_IMAGE_BYTES", 10)
    for response in [
        httpx.Response(200, content=picture()),
        httpx.Response(200, headers={"content-length": "11"}),
        httpx.Response(302, headers={"location": "/again"}),
        httpx.Response(500),
    ]:
        reader = download.QQImageDownloader(
            transport=httpx.MockTransport(lambda r, value=response: value)
        )
        with pytest.raises(MediaError):
            await reader.fetch("https://qpic.cn/a")
