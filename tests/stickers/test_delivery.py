"""出站归档显式接受已校验表情动图，普通素材上传仍保持原限制。"""

from types import SimpleNamespace

import pytest

from agent_bot.integrations.onebot.outgoing import DeliveryScope, OutgoingRecorder
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.errors import MediaError
from materials.test_store import material_database as material_database
from materials.test_store import materials as materials
from stickers.test_images import picture


@pytest.mark.parametrize("format,mime,extension", [
    ("GIF", "image/gif", "gif"),
    ("WEBP", "image/webp", "webp"),
    ("PNG", "image/png", "png"),
])
async def test_outgoing_sticker_archive_retains_animation_but_normal_upload_rejects_it(
    materials, format, mime, extension,
):
    """同一动图只在内部出站入口开放，模型识图和普通上传不被放宽。"""
    original = picture(format, animated=True)
    with pytest.raises(MediaError) as rejected:
        await materials.put(owner=MediaOwner(entry="qq", session_id="s"), name="动图",
                            mime_type=mime, data=original)
    assert rejected.value.code == "MEDIA_ANIMATION_UNSUPPORTED"
    recorder = OutgoingRecorder(
        service_provider=lambda: None, store_provider=lambda: None,
        materials_provider=lambda: materials, scope_provider=lambda _: SimpleNamespace(),
    )
    item = await recorder._material(
        DeliveryScope("s", "generation", "request"), original, mime, f"sticker.{extension}",
    )
    owner = MediaOwner(entry="qq", session_id="s")
    async with materials.open_content(item.id, owner=owner) as file:
        assert file.read() == original
    assert item.mime_type == mime
