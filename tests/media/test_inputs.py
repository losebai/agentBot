"""真实图片解码、脱敏和有界模型副本的回归。"""

from io import BytesIO

import pytest
from PIL import Image

from agent_bot.media.errors import MediaError


def picture(format="PNG", *, size=(12, 9), **options):
    """只生成小型离线样本，不下载真实素材。"""
    output = BytesIO()
    with Image.new("RGB", size, "red") as image:
        image.save(output, format=format, **options)
    return output.getvalue()


@pytest.mark.parametrize(
    "format,mime",
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp"), ("GIF", "image/gif")],
)
def test_prepare_valid_image_removes_metadata(format, mime):
    """原图保留给工作流；模型副本经过独立重新编码。"""
    from agent_bot.media.inputs import ImagePreparer

    raw = picture(format)
    result = ImagePreparer().prepare(raw, mime)
    assert result.data == raw and result.media_type == mime
    assert result.model_image.source_size_bytes == len(raw)
    assert len(result.model_image.data) <= 2 * 1024 * 1024
    with Image.open(BytesIO(result.model_image.data)) as model:
        assert model.getexif() == {}
        assert model.size == (12, 9)


def test_reject_mime_truncation_animation_and_pixels(monkeypatch):
    """尺寸边界通过替换常量验证，不分配像素炸弹。"""
    from agent_bot.media import inputs

    preparer = inputs.ImagePreparer()
    for raw, mime in [
        (picture(), "image/jpeg"),
        (picture()[:25], "image/png"),
        (b"<svg></svg>", "image/svg+xml"),
        (b"", None),
    ]:
        with pytest.raises(MediaError):
            preparer.prepare(raw, mime)
    with Image.new("RGB", (12, 9), "blue") as second:
        animated = picture("GIF", save_all=True, append_images=[second], duration=10)
    with pytest.raises(MediaError, match="MEDIA_ANIMATION_UNSUPPORTED"):
        preparer.prepare(animated, "image/gif")
    monkeypatch.setattr(inputs, "MAX_MODEL_IMAGE_PIXELS", 100)
    with pytest.raises(MediaError):
        preparer.prepare(picture(), None)


def test_combined_original_bytes_and_count(monkeypatch):
    """总量按原始数据累计，不能用压缩后的副本绕过。"""
    from agent_bot.media import inputs

    preparer = inputs.ImagePreparer()
    result = preparer.prepare(picture(), None)
    monkeypatch.setattr(inputs, "MAX_TOTAL_IMAGE_BYTES", len(result.data) * 2 - 1)
    with pytest.raises(MediaError, match="MEDIA_TOO_LARGE"):
        preparer.prepare_many([result, result], maximum_count=4)
    with pytest.raises(MediaError, match="MEDIA_TOO_MANY_IMAGES"):
        preparer.prepare_many([result] * 9, maximum_count=20)
    assert preparer.prepare_many([result], maximum_count=1) == (result.model_image,)
