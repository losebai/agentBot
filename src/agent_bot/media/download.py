"""使用固定公网连接下载可信 QQ 素材，不泄漏签名 URL。"""

import asyncio
from urllib.parse import urljoin

import httpx

from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.public_http import PublicTransport
from agent_bot.media.config import MAX_IMAGE_BYTES
from agent_bot.media.errors import MediaError
from agent_bot.media.inputs import ImagePreparer
from agent_bot.multimodal import InvalidImageAttachmentError, validate_qq_image_download_url


class QQImageDownloader:
    """每次下载独立有界连接，禁止代理、重试和自动重定向。"""

    def __init__(self, *, transport=None, remaining=None, preparer=None):
        """测试可注入传输，生产路径使用经过验证的数字地址。"""
        self.transport = transport
        self.remaining = remaining or (lambda: 15.0)
        self.preparer = preparer or ImagePreparer()

    def make_transport(self):
        """连接层保留原 Host 与 TLS SNI，DNS 检查不与连接分离。"""
        return self.transport or PublicTransport()

    async def fetch(self, url: str):
        """重定向、流读取和图片准备共同计入较短的剩余预算。"""
        try:
            return await asyncio.wait_for(self._fetch(url), min(15.0, self.remaining()))
        except (InvalidImageAttachmentError, AgentError) as error:
            raise MediaError("MEDIA_IMAGE_URL_REJECTED", status_code=422) from error
        except (httpx.HTTPError, ValueError, TimeoutError) as error:
            raise MediaError("MEDIA_IMAGE_DOWNLOAD_FAILED") from error

    async def _fetch(self, url):
        """每跳重新校验域名，实际接收量和声明长度分别约束。"""
        current = validate_qq_image_download_url(url)
        async with httpx.AsyncClient(
            transport=self.make_transport(), trust_env=False, follow_redirects=False, timeout=15
        ) as client:
            for redirect in range(3):
                async with client.stream(
                    "GET", current, headers={"Accept-Encoding": "identity"}
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect == 2:
                            raise MediaError("MEDIA_IMAGE_DOWNLOAD_FAILED")
                        current = validate_qq_image_download_url(urljoin(current, location))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise MediaError("MEDIA_IMAGE_DOWNLOAD_FAILED")
                    size = response.headers.get("content-length")
                    if size is not None and not 0 <= int(size) <= MAX_IMAGE_BYTES:
                        raise MediaError("MEDIA_TOO_LARGE", status_code=413)
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(data) + len(chunk) > MAX_IMAGE_BYTES:
                            raise MediaError("MEDIA_TOO_LARGE", status_code=413)
                        data.extend(chunk)
                    return await self.preparer.prepare_async(
                        bytes(data), response.headers.get("content-type")
                    )
        raise MediaError("MEDIA_IMAGE_DOWNLOAD_FAILED")
