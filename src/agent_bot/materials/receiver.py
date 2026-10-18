"""OneBot 图片和文件登记：引用复用原件，远程下载有界且禁止读取任意本地路径。"""

import asyncio
import hashlib
import json
from urllib.parse import urljoin

import httpx

from agent_bot.agent.tools.public_http import PublicTransport
from agent_bot.materials.contracts import MAX_FILE_BYTES, MaterialView
from agent_bot.media.download import QQImageDownloader
from agent_bot.media.errors import MediaError
from agent_bot.multimodal import validate_public_image_url


class FileDownloader:
    """连接时固定公开 IP，每跳重新校验 URL，不使用环境代理。"""

    def __init__(self, transport=None):
        """测试可替换传输层，真实环境复用已验证的公开网络传输。"""
        self.transport = transport

    async def fetch(self, url: str) -> tuple[bytes, str]:
        """总下载期限十五秒，声明长度和实际字节均不得超过二十 MiB。"""
        try:
            return await asyncio.wait_for(self._fetch(url), 15)
        except (httpx.HTTPError, ValueError, TimeoutError) as error:
            raise MediaError("MATERIAL_DOWNLOAD_FAILED") from error

    async def _fetch(self, url: str) -> tuple[bytes, str]:
        """流式接收原件；不信任 OneBot 事件提供的文件路径或 HTTP 重定向。"""
        current = validate_public_image_url(url)
        async with httpx.AsyncClient(
            transport=self.transport or PublicTransport(), timeout=15,
            trust_env=False, follow_redirects=False,
        ) as client:
            for redirect in range(3):
                async with client.stream(
                    "GET", current, headers={"Accept-Encoding": "identity"},
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect == 2:
                            raise MediaError("MATERIAL_DOWNLOAD_FAILED")
                        current = validate_public_image_url(urljoin(current, location))
                        continue
                    if not 200 <= response.status_code < 300:
                        raise MediaError("MATERIAL_DOWNLOAD_FAILED")
                    size = response.headers.get("content-length")
                    if size is not None and not 0 < int(size) <= MAX_FILE_BYTES:
                        raise MediaError("MEDIA_TOO_LARGE", status_code=413)
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        if len(data) + len(chunk) > MAX_FILE_BYTES:
                            raise MediaError("MEDIA_TOO_LARGE", status_code=413)
                        data.extend(chunk)
                    mime = response.headers.get("content-type", "application/octet-stream")
                    return bytes(data), mime.split(";", 1)[0].strip().lower()
        raise MediaError("MATERIAL_DOWNLOAD_FAILED")


class MaterialReceiver:
    """接收与引用共用来源标识，相同会话内不重复下载已经保存的附件。"""

    def __init__(self, store, *, image_downloader=None, file_downloader=None):
        """依赖由进程资源注入，构造不联网。"""
        self.store = store
        self.images = image_downloader or QQImageDownloader()
        self.files = file_downloader or FileDownloader()

    async def register(self, bot, owner, segment, *, group_id: int | None) -> MaterialView:
        """只登记图片和普通文件，归属只能由事件外层的可信会话提供。"""
        if segment.type not in {"image", "file"}:
            raise ValueError("unsupported material segment")
        data = segment.data
        # 普通文件的 file 往往只是显示名称；同名文件不能复用另一份原件。
        identity = (
            data.get("file_id") or data.get("file") or data.get("url")
            if segment.type == "image"
            else data.get("file_id") or data.get("id") or data.get("url")
        )
        if not isinstance(identity, str) or not identity:
            raise MediaError("MATERIAL_INVALID_FILE", status_code=422)
        source = hashlib.sha256(json.dumps(
            (str(bot.self_id), segment.type, identity), ensure_ascii=False,
        ).encode()).hexdigest()
        old = await self.store.find_source(owner, source)
        if old is not None:
            return old
        reported = data.get("file_size", data.get("size"))
        if reported is not None:
            try:
                valid_size = 0 < int(reported) <= MAX_FILE_BYTES
            except (ValueError, TypeError):
                valid_size = False
            if not valid_size:
                raise MediaError("MEDIA_TOO_LARGE", status_code=413)
        if segment.type == "image":
            image = await self.images.fetch(data.get("url") or data.get("file"))
            content, mime = image.data, image.media_type
            name = data.get("name") or data.get("file") or "图片"
        else:
            name = data.get("name") or data.get("file_name") or data.get("file") or "文件"
            url = data.get("url")
            if not url:
                file_id = data.get("file_id") or data.get("id")
                if not isinstance(file_id, str) or not file_id:
                    raise MediaError("MATERIAL_FILE_ID_REQUIRED", status_code=422)
                if group_id is None:
                    response = await bot.call_api("get_private_file_url", file_id=file_id)
                else:
                    params = {"group_id": group_id, "file_id": file_id}
                    if data.get("busid") is not None:
                        params["busid"] = int(data["busid"])
                    response = await bot.call_api("get_group_file_url", **params)
                if not isinstance(response, dict) or not isinstance(response.get("url"), str):
                    raise MediaError("MATERIAL_DOWNLOAD_FAILED")
                url = response["url"]
            content, mime = await self.files.fetch(url)
        return await self.store.put(
            owner=owner, name=name, mime_type=mime, data=content, source_key=source,
        )
