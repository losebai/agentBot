"""OneBot 图片素材提取与安全校验。"""

from __future__ import annotations

import base64
import binascii
import math
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from io import BytesIO
from ipaddress import ip_address
from urllib.parse import urljoin, urlsplit

import httpx
from nonebot.adapters.onebot.v11 import Message
from PIL import Image, ImageOps, UnidentifiedImageError

DEFAULT_IMAGE_PROMPT = "请识别并描述图片内容；如包含文字，请一并提取。"
MAX_IMAGE_URL_LENGTH = 4096
MAX_IMAGES_PER_MESSAGE = 8
MAX_VIDEOS_PER_MESSAGE = 2
MAX_REPORTED_IMAGE_BYTES = 10 * 1024 * 1024
MAX_REPORTED_VIDEO_BYTES = 100 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
MAX_MODEL_IMAGE_BYTES = 2 * 1024 * 1024
MAX_MODEL_IMAGE_PIXELS = 16 * 1024 * 1024
MAX_IMAGE_REDIRECTS = 2
IMAGE_DOWNLOAD_TIMEOUT_SECONDS = 15.0

_SUPPORTED_IMAGE_MEDIA_TYPES = frozenset(
    {"image/gif", "image/jpeg", "image/png", "image/webp"}
)
_TRUSTED_QQ_MEDIA_DOMAINS = ("qpic.cn", "qq.com", "qq.com.cn")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_BASE64_CHARS = ((MAX_REPORTED_IMAGE_BYTES + 2) // 3) * 4
MAX_IMAGE_DATA_URL_LENGTH = len("data:image/jpeg;base64,") + _MAX_BASE64_CHARS


class ImageAttachmentError(ValueError):
    """图片素材不可用于当前多模态请求。"""


class ImageRecognitionDisabledError(ImageAttachmentError):
    """管理员关闭了图片识别。"""


class InvalidImageAttachmentError(ImageAttachmentError):
    """OneBot 图片段没有安全、可转发的公开 URL。"""


class TooManyImagesError(ImageAttachmentError):
    """单条消息的图片数量超过配置上限。"""


class VideoAttachmentError(ValueError):
    """视频素材不可用于当前多模态请求。"""


class InvalidVideoAttachmentError(VideoAttachmentError):
    """OneBot 视频段没有安全、可访问的在线视频 URL。"""


class TooManyVideosError(VideoAttachmentError):
    """单条消息的视频数量超过服务端上限。"""


@dataclass(frozen=True, slots=True)
class MultimodalInput:
    prompt: str
    image_urls: tuple[str, ...]
    video_urls: tuple[str, ...] = ()


def validate_public_image_url(value: str) -> str:
    """只接受不含凭据、片段或 IP 字面量的公开 HTTP(S) URL。"""

    if not isinstance(value, str) or not value or len(value) > MAX_IMAGE_URL_LENGTH:
        raise InvalidImageAttachmentError("image URL length is invalid")
    if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
        raise InvalidImageAttachmentError("image URL contains control characters")

    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise InvalidImageAttachmentError("image URL is malformed") from exc

    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port == 0
    ):
        raise InvalidImageAttachmentError("image URL is not a public HTTP(S) URL")

    normalized_host = host.rstrip(".").casefold()
    if normalized_host == "localhost" or normalized_host.endswith(
        (".localhost", ".local", ".internal")
    ):
        raise InvalidImageAttachmentError("local image hosts are not allowed")
    if not any(character.isalpha() for character in normalized_host):
        raise InvalidImageAttachmentError("numeric image hosts are not allowed")
    try:
        ip_address(normalized_host)
    except ValueError:
        pass
    else:
        raise InvalidImageAttachmentError("IP literal image hosts are not allowed")

    return value


def validate_qq_image_download_url(value: str) -> str:
    """仅允许从可信 QQ/Tencent 图片 CDN 通过 HTTPS 下载素材。"""

    validated = validate_public_image_url(value)
    parsed = urlsplit(validated)
    host = parsed.hostname
    assert host is not None
    normalized_host = host.rstrip(".").casefold()
    if parsed.scheme.casefold() != "https" or not any(
        normalized_host == domain or normalized_host.endswith(f".{domain}")
        for domain in _TRUSTED_QQ_MEDIA_DOMAINS
    ):
        raise InvalidImageAttachmentError("image download host is not trusted")
    return validated


def validate_qq_video_url(value: str) -> str:
    """只接受 NapCat 返回的可信 QQ/Tencent HTTPS 视频地址。"""

    try:
        validated = validate_public_image_url(value)
    except InvalidImageAttachmentError as exc:
        raise InvalidVideoAttachmentError("video URL is invalid") from exc
    parsed = urlsplit(validated)
    host = parsed.hostname
    assert host is not None
    normalized_host = host.rstrip(".").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or parsed.port not in {None, 443}
        or not any(
            normalized_host == domain or normalized_host.endswith(f".{domain}")
            for domain in _TRUSTED_QQ_MEDIA_DOMAINS
        )
    ):
        raise InvalidVideoAttachmentError("video URL host is not trusted")
    return validated


def _validate_image_content(content: bytes, content_type: str | None) -> str:
    """共享素材大小、签名和 MIME 校验，返回真实 MIME。"""

    if not content or len(content) > MAX_REPORTED_IMAGE_BYTES:
        raise InvalidImageAttachmentError("downloaded image size is invalid")
    detected_type = _detect_image_media_type(content)
    if detected_type is None:
        raise InvalidImageAttachmentError("downloaded content is not a supported image")

    reported_type = (content_type or "").partition(";")[0].strip().casefold()
    if reported_type == "image/jpg":
        reported_type = "image/jpeg"
    if reported_type not in {
        "",
        "application/octet-stream",
        "binary/octet-stream",
        detected_type,
    }:
        raise InvalidImageAttachmentError("downloaded image content type is invalid")

    return detected_type


def image_bytes_to_data_url(
    content: bytes,
    *,
    content_type: str | None,
    max_bytes: int = MAX_MODEL_IMAGE_BYTES,
) -> str:
    """校验图片并编码为不超过模型请求体预算的 data URL。"""
    media_type = _validate_image_content(content, content_type)
    normalized_type, normalized = _normalize_model_image(
        content,
        media_type=media_type,
        max_bytes=max_bytes,
    )
    encoded = base64.b64encode(normalized).decode("ascii")
    media_type = normalized_type
    return f"data:{media_type};base64,{encoded}"


def _normalize_model_image(
    content: bytes,
    *,
    media_type: str,
    max_bytes: int,
) -> tuple[str, bytes]:
    """将超过预算的图片缩放为 JPEG，防止 Base64 请求超过代理限制。"""

    if max_bytes <= 0:
        raise InvalidImageAttachmentError("model image budget is exhausted")
    if len(content) <= max_bytes:
        return media_type, content

    try:
        with Image.open(BytesIO(content)) as source:
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_MODEL_IMAGE_PIXELS:
                raise InvalidImageAttachmentError("image dimensions are unsupported")
            source.seek(0)
            oriented = ImageOps.exif_transpose(source)
            rgba = oriented.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
            rgba.close()
    except InvalidImageAttachmentError:
        raise
    except (Image.DecompressionBombError, OSError, UnidentifiedImageError) as exc:
        raise InvalidImageAttachmentError("image cannot be normalized") from exc

    try:
        for _ in range(6):
            smallest: bytes | None = None
            for quality in (85, 75, 65, 55):
                output = BytesIO()
                image.save(output, format="JPEG", quality=quality, optimize=True)
                candidate = output.getvalue()
                if len(candidate) <= max_bytes:
                    return "image/jpeg", candidate
                smallest = candidate

            assert smallest is not None
            scale = min(0.9, math.sqrt(max_bytes / len(smallest)) * 0.9)
            resized = (
                max(1, round(image.width * scale)),
                max(1, round(image.height * scale)),
            )
            if resized == image.size:
                break
            replacement = image.resize(resized, Image.Resampling.LANCZOS)
            image.close()
            image = replacement
    finally:
        image.close()
    raise InvalidImageAttachmentError("image cannot fit the model request budget")


@dataclass(frozen=True, slots=True)
class DownloadedImage:
    """通过受限 CDN 下载且校验后的图片，不在 repr 暴露素材。"""

    media_type: str
    data: bytes = field(repr=False)

    def validate_static(self) -> None:
        """按容器块边界拒绝动画标记，不改动已有识图路径。"""
        if self.media_type == "image/jpeg":
            return
        if self.media_type not in {"image/png", "image/webp"}:
            raise InvalidImageAttachmentError("image editing requires a static image")
        is_png = self.media_type == "image/png"
        offset = 8 if is_png else 12
        overhead = 12 if is_png else 8
        while offset + overhead <= len(self.data):
            size_start = offset if is_png else offset + 4
            length = int.from_bytes(
                self.data[size_start:size_start + 4], "big" if is_png else "little",
            )
            kind = self.data[offset + 4:offset + 8] if is_png else self.data[offset:offset + 4]
            if kind in {b"acTL", b"ANIM", b"ANMF"} or (
                kind == b"VP8X" and length > 0 and self.data[offset + 8] & 2
            ):
                raise InvalidImageAttachmentError("animated image editing is unsupported")
            offset += overhead + length + (0 if is_png else length % 2)


async def fetch_image_attachment(
    client: httpx.AsyncClient, image_url: str, *, max_bytes: int,
) -> DownloadedImage:
    """复用识图下载边界获取原始字节，不把 Base64 放入后台队列。"""
    content, content_type = await _download_image(
        client, image_url, max_bytes=min(max_bytes, MAX_REPORTED_IMAGE_BYTES),
    )
    return DownloadedImage(_validate_image_content(content, content_type), content)


def validate_image_reference(value: str) -> str:
    """校验发往 dispatch-ai 的远程 URL 或 Base64 图片引用。"""

    if isinstance(value, str) and value.startswith("data:"):
        return _validate_image_data_url(value)
    return validate_public_image_url(value)


async def fetch_image_data_urls(
    client: httpx.AsyncClient,
    image_urls: Sequence[str],
) -> tuple[str, ...]:
    """顺序下载受信任的 QQ 图片，在内存上限内转换为 Base64 data URL。"""

    if len(image_urls) > MAX_IMAGES_PER_MESSAGE:
        raise TooManyImagesError(
            f"a message can contain at most {MAX_IMAGES_PER_MESSAGE} images"
        )

    remaining_bytes = MAX_TOTAL_IMAGE_BYTES
    model_image_budget = MAX_MODEL_IMAGE_BYTES // max(1, len(image_urls))
    data_urls: list[str] = []
    for image_url in image_urls:
        content, content_type = await _download_image(
            client,
            image_url,
            max_bytes=min(MAX_REPORTED_IMAGE_BYTES, remaining_bytes),
        )
        remaining_bytes -= len(content)
        data_urls.append(
            image_bytes_to_data_url(
                content,
                content_type=content_type,
                max_bytes=model_image_budget,
            )
        )
    return tuple(data_urls)


async def _download_image(
    client: httpx.AsyncClient,
    image_url: str,
    *,
    max_bytes: int,
) -> tuple[bytes, str | None]:
    if max_bytes <= 0:
        raise InvalidImageAttachmentError("combined image size is too large")

    current_url = validate_qq_image_download_url(image_url)
    for _ in range(MAX_IMAGE_REDIRECTS + 1):
        redirect_url: str | None = None
        try:
            async with client.stream(
                "GET",
                current_url,
                headers={"User-Agent": "dispatch-chat/image-fetch"},
                timeout=httpx.Timeout(IMAGE_DOWNLOAD_TIMEOUT_SECONDS),
            ) as response:
                if response.status_code in _REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise InvalidImageAttachmentError("image redirect is invalid")
                    redirect_url = validate_qq_image_download_url(
                        urljoin(current_url, location)
                    )
                elif response.status_code < 200 or response.status_code >= 300:
                    raise InvalidImageAttachmentError("image download failed")
                else:
                    content_length = response.headers.get("Content-Length")
                    if content_length is not None:
                        try:
                            reported_length = int(content_length)
                        except ValueError as exc:
                            raise InvalidImageAttachmentError(
                                "image content length is invalid"
                            ) from exc
                        if reported_length < 0 or reported_length > max_bytes:
                            raise InvalidImageAttachmentError("downloaded image is too large")

                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > max_bytes:
                            raise InvalidImageAttachmentError("downloaded image is too large")
                        content.extend(chunk)
                    return bytes(content), response.headers.get("Content-Type")
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            raise InvalidImageAttachmentError("image download is unavailable") from exc

        if redirect_url is None:
            break
        current_url = redirect_url
    raise InvalidImageAttachmentError("image redirected too many times")


def _validate_image_data_url(value: str) -> str:
    if not value or len(value) > MAX_IMAGE_DATA_URL_LENGTH:
        raise InvalidImageAttachmentError("image data URL length is invalid")
    header, separator, encoded = value.partition(",")
    if not separator or not encoded or not header.endswith(";base64"):
        raise InvalidImageAttachmentError("image data URL is malformed")
    media_type = header.removeprefix("data:").removesuffix(";base64").casefold()
    if media_type not in _SUPPORTED_IMAGE_MEDIA_TYPES:
        raise InvalidImageAttachmentError("image data URL type is unsupported")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InvalidImageAttachmentError("image data URL payload is invalid") from exc
    if (
        not content
        or len(content) > MAX_REPORTED_IMAGE_BYTES
        or _detect_image_media_type(content) != media_type
    ):
        raise InvalidImageAttachmentError("image data URL content is invalid")
    return value


def _detect_image_media_type(content: bytes) -> str | None:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    return None


def parse_multimodal_input(
    message: Message,
    *,
    enabled: bool,
    max_images: int,
) -> MultimodalInput:
    """提取文本和当前消息的多张图片 URL，不下载或持久化图片。"""

    if not 1 <= max_images <= MAX_IMAGES_PER_MESSAGE:
        raise ValueError("max_images is outside the supported range")

    image_segments = [segment for segment in message if segment.type == "image"]
    if image_segments and not enabled:
        raise ImageRecognitionDisabledError("image recognition is disabled")
    if len(image_segments) > max_images:
        raise TooManyImagesError(f"a message can contain at most {max_images} images")

    image_urls: list[str] = []
    seen_urls: set[str] = set()
    for segment in image_segments:
        reported_size = segment.data.get("file_size")
        if reported_size is not None:
            try:
                normalized_size = int(reported_size)
            except (TypeError, ValueError) as exc:
                raise InvalidImageAttachmentError("image size is invalid") from exc
            if normalized_size < 0 or normalized_size > MAX_REPORTED_IMAGE_BYTES:
                raise InvalidImageAttachmentError("image is too large")
        candidate = segment.data.get("url") or segment.data.get("file")
        if not isinstance(candidate, str):
            raise InvalidImageAttachmentError("image segment does not contain a remote URL")
        image_url = validate_public_image_url(candidate)
        if image_url not in seen_urls:
            seen_urls.add(image_url)
            image_urls.append(image_url)

    prompt = message.extract_plain_text().strip()
    if image_urls and not prompt:
        prompt = DEFAULT_IMAGE_PROMPT
    return MultimodalInput(prompt=prompt, image_urls=tuple(image_urls))
