"""表情包独立图片边界与动画保留验证。"""

from io import BytesIO

import pytest
from PIL import Image

from agent_bot.media.errors import MediaError
from agent_bot.plugins.stickers.images import prepare_sticker_image


def picture(format='PNG', *, animated=False):
    """生成小型离线图片，动画含实际不同帧。"""
    output = BytesIO()
    first = Image.new('RGB', (24, 20), 'red')
    first.save(output, format=format, save_all=animated,
               append_images=[Image.new('RGB', first.size, 'blue')] if animated else [])
    return output.getvalue()


@pytest.mark.parametrize('format,mime', [
    ('GIF', 'image/gif'), ('WEBP', 'image/webp'), ('PNG', 'image/png'),
])
def test_animation_original_and_static_png_preview(format, mime):
    """识图预览与发送原件分离，各动画容器的原始帧不会丢失。"""
    original = picture(format, animated=True)
    prepared = prepare_sticker_image(original, mime)
    assert prepared.data == original
    assert prepared.mime_type == mime
    assert prepared.animated and prepared.frame_count == 2
    assert (prepared.width, prepared.height) == (24, 20)
    with Image.open(BytesIO(prepared.preview)) as preview:
        assert preview.format == 'PNG' and not getattr(preview, 'is_animated', False)
    assert len(prepared.preview) <= 2 * 1024 * 1024


@pytest.mark.parametrize('data,mime,code', [
    (b'not an image', 'image/png', 'STICKER_INVALID_IMAGE'),
    (b'not an image', 'application/octet-stream', 'STICKER_INVALID_IMAGE'),
    (b'x' * (10 * 1024 * 1024 + 1), None, 'STICKER_TOO_LARGE'),
], ids=['container','binary','size'])
def test_invalid_container_mime_and_size(data, mime, code):
    """不依赖声明类型，损坏容器与超限原件仍提供稳定错误。"""
    with pytest.raises(MediaError) as error:
        prepare_sticker_image(data, mime)
    assert error.value.code == code


@pytest.mark.parametrize('declared', ['image/jpeg', 'application/octet-stream', None])
def test_mislabeled_webp_uses_decoded_mime(declared):
    """错误后缀和缺省声明不能拒绝合法动图，存储类型取自实际解码。"""
    original = picture('WEBP', animated=True)
    prepared = prepare_sticker_image(original, declared)
    assert prepared.data == original and prepared.mime_type == 'image/webp'
    assert prepared.animated and prepared.frame_count == 2


@pytest.mark.parametrize('budget,value,code', [
    ('MAX_FRAMES', 1, 'STICKER_TOO_MANY_FRAMES'),
    ('MAX_FRAME_PIXELS', 479, 'STICKER_PIXEL_BUDGET_EXCEEDED'),
    ('MAX_DECODED_PIXELS', 959, 'STICKER_PIXEL_BUDGET_EXCEEDED'),
])
def test_independent_frame_and_total_decoding_budgets(monkeypatch, budget, value, code):
    """实际解码不同帧检验三个独立预算，不能只检查首帧或压缩后大小。"""
    monkeypatch.setattr('agent_bot.plugins.stickers.images.' + budget, value)
    with pytest.raises(MediaError, match=code):
        prepare_sticker_image(picture('GIF', animated=True), 'image/gif')


def test_preview_shrinks_to_png_limit_without_changing_original(monkeypatch):
    """生成预览的缩放分支始终以原件为发送内容。"""
    monkeypatch.setattr('agent_bot.plugins.stickers.images.MAX_PREVIEW_BYTES', 80)
    original = picture('GIF', animated=True)
    prepared = prepare_sticker_image(original, 'image/gif')
    assert len(prepared.preview) <= 80 and prepared.data == original
