"""生成结果必须为唯一且严格匹配请求格式的二进制附件。"""

import base64

import pytest

from agent_bot.media.errors import MediaError
from agent_bot.models import ChatMediaItem, ChatMediaResponse
from media.test_inputs import picture


def response(raw, mime="image/png", kind="IMAGE"):
    """构建无外部调用的媒体响应。"""
    return ChatMediaResponse(
        items=(ChatMediaItem(type=kind, mime_type=mime, data=base64.b64encode(raw).decode()),)
    )


def test_generated_image_is_actually_decoded():
    """魔数正确但图片截断也必须拒绝。"""
    from agent_bot.media.validation import validate_generated_media

    assert (
        validate_generated_media(
            response(picture()), output_type="IMAGE", format="png", max_bytes=1024
        )
        == picture()
    )
    for sample in [
        response(picture()[:16]),
        response(picture(), "image/jpeg"),
        response(picture()).model_copy(update={"items": ()}),
    ]:
        with pytest.raises(MediaError):
            validate_generated_media(sample, output_type="IMAGE", format="png", max_bytes=1024)
    with pytest.raises(MediaError):
        validate_generated_media(
            response(picture()), output_type="IMAGE", format="png", max_bytes=10
        )


@pytest.mark.parametrize(
    "format,mime,raw",
    [
        ("pcm", "audio/pcm", b"\x00\x01"),
        ("mp3", "audio/mpeg", b"ID3\x00"),
        ("wav", "audio/wav", b"RIFF\x04\x00\x00\x00WAVE"),
        ("flac", "audio/flac", b"fLaC\x00"),
        ("opus", "audio/ogg", b"OggSOpusHead"),
        ("aac", "audio/aac", b"\xff\xf1\x00"),
    ],
)
def test_generated_audio_signature(format, mime, raw):
    """容器签名与 PCM 对齐只证明格式边界，不声称音频内容有效。"""
    from agent_bot.media.validation import validate_generated_media

    assert (
        validate_generated_media(
            response(raw, mime, "AUDIO"), output_type="AUDIO", format=format, max_bytes=1024
        )
        == raw
    )
    with pytest.raises(MediaError):
        validate_generated_media(
            response(b"x", mime, "AUDIO"), output_type="AUDIO", format=format, max_bytes=1024
        )
