import base64
import random
import struct
import zlib

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from agent_bot.multimodal import (
    ImageRecognitionDisabledError,
    InvalidImageAttachmentError,
    TooManyImagesError,
    image_bytes_to_data_url,
    parse_multimodal_input,
    validate_image_reference,
    validate_qq_video_url,
)


def image_segment(url: str) -> MessageSegment:
    return MessageSegment(type="image", data={"url": url})


def test_extracts_text_and_multiple_public_image_urls() -> None:
    message = Message(
        [
            MessageSegment.text("请比较这两张图"),
            image_segment("https://gchat.qpic.cn/first.jpg?token=one"),
            image_segment("https://multimedia.nt.qq.com.cn/second.png?token=two"),
        ]
    )

    result = parse_multimodal_input(message, enabled=True, max_images=4)

    assert result.prompt == "请比较这两张图"
    assert result.image_urls == (
        "https://gchat.qpic.cn/first.jpg?token=one",
        "https://multimedia.nt.qq.com.cn/second.png?token=two",
    )


def test_image_only_message_uses_a_default_recognition_prompt() -> None:
    result = parse_multimodal_input(
        Message(image_segment("https://gchat.qpic.cn/only.jpg")),
        enabled=True,
        max_images=4,
    )

    assert result.prompt == "请识别并描述图片内容；如包含文字，请一并提取。"
    assert result.image_urls == ("https://gchat.qpic.cn/only.jpg",)


def test_rejects_disabled_or_excessive_image_recognition() -> None:
    message = Message(image_segment("https://gchat.qpic.cn/one.jpg"))
    with pytest.raises(ImageRecognitionDisabledError):
        parse_multimodal_input(message, enabled=False, max_images=4)

    too_many = Message(
        [
            image_segment("https://gchat.qpic.cn/one.jpg"),
            image_segment("https://gchat.qpic.cn/two.jpg"),
            image_segment("https://gchat.qpic.cn/three.jpg"),
        ]
    )
    with pytest.raises(TooManyImagesError, match="2"):
        parse_multimodal_input(too_many, enabled=True, max_images=2)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/private.png",
        "data:image/png;base64,AAAA",
        "http://127.0.0.1/private.png",
        "http://2130706433/private.png",
        "http://127.1/private.png",
        "http://[::1]/private.png",
        "https://user:password@example.com/private.png",
        "https://example.com/image.png#fragment",
    ],
)
def test_rejects_unsafe_image_urls(url: str) -> None:
    with pytest.raises(InvalidImageAttachmentError):
        parse_multimodal_input(Message(image_segment(url)), enabled=True, max_images=4)


@pytest.mark.parametrize(
    "url",
    [
        "http://gzc-download.ftn.qq.com/video.mp4",
        "https://attacker.example/video.mp4",
        "https://gzc-download.ftn.qq.com:8443/video.mp4",
    ],
)
def test_rejects_video_urls_outside_the_trusted_https_origin(url: str) -> None:
    with pytest.raises(ValueError):
        validate_qq_video_url(url)


def test_rejects_image_with_excessive_reported_size() -> None:
    segment = MessageSegment(
        type="image",
        data={
            "url": "https://gchat.qpic.cn/oversized.jpg",
            "file_size": 10 * 1024 * 1024 + 1,
        },
    )

    with pytest.raises(InvalidImageAttachmentError):
        parse_multimodal_input(Message(segment), enabled=True, max_images=4)


def test_encodes_validated_image_bytes_as_data_url() -> None:
    value = image_bytes_to_data_url(
        b"\x89PNG\r\n\x1a\nimage-bytes",
        content_type="image/png; charset=binary",
    )

    assert value == "data:image/png;base64,iVBORw0KGgppbWFnZS1ieXRlcw=="
    assert validate_image_reference(value) == value


def test_reduces_large_image_before_embedding_it_in_model_request() -> None:
    """大图不能再把 dispatch-ai 请求体推过反向代理限制。"""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    width = height = 1024
    pixels = random.Random(0).randbytes(width * height * 3)
    scanlines = b"".join(
        b"\0" + pixels[offset:offset + width * 3]
        for offset in range(0, len(pixels), width * 3)
    )
    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanlines))
        + chunk(b"IEND", b"")
    )

    value = image_bytes_to_data_url(png, content_type="image/png")
    normalized = base64.b64decode(value.partition(",")[2], validate=True)

    assert len(png) > 2 * 1024 * 1024
    assert value.startswith("data:image/jpeg;base64,")
    assert len(normalized) <= 2 * 1024 * 1024


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"<svg></svg>", "image/svg+xml"),
        (b"\x89PNG\r\n\x1a\nimage", "image/jpeg"),
        (b"not-an-image", "application/octet-stream"),
    ],
)
def test_rejects_unsupported_or_mismatched_downloaded_images(
    content: bytes,
    content_type: str,
) -> None:
    with pytest.raises(InvalidImageAttachmentError):
        image_bytes_to_data_url(content, content_type=content_type)
