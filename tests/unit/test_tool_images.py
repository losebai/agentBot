import base64

import pytest

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()


def test_collection_enforces_total_size_and_deduplicates():
    """多个合法附件仍需受总大小限制，重复图片不多次发送。"""
    from agent_bot.media_artifacts import ImageArtifact
    from agent_bot.services.tool_images import ToolImageCollection

    collection = ToolImageCollection()
    image = ImageArtifact(mimeType="image/png", data=PNG)
    collection.add((image, image))
    assert collection.items == (image,)
    large = ImageArtifact(mimeType="image/png", data=base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"x" * 10_485_752,
    ).decode())
    with pytest.raises(ValueError):
        collection.add((large,))
    assert collection.items == (image,)
