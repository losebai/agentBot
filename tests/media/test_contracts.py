"""验证私有媒体数据与公开协议之间的边界。"""

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError


def test_private_media_bytes_are_not_serialized():
    """图片内容不因日志 repr 或嵌套序列化泄漏。"""
    from agent_bot.media.contracts import ModelImage, ValidatedImage

    image = ModelImage(media_type="image/png", data=b"private-image", source_size_bytes=20)
    value = ValidatedImage(media_type="image/png", data=b"private-original", model_image=image)
    assert "data" not in image.model_dump()
    assert "private" not in repr(value)
    assert "private" not in value.model_dump_json()


def test_media_limits_and_defaults():
    """能力默认关闭，字节和标识有确定的硬上限。"""
    from agent_bot.ai.contracts import ModelMediaCapabilities
    from agent_bot.media.config import MAX_IMAGE_BYTES, MAX_MODEL_IMAGE_BYTES
    from agent_bot.media.contracts import MediaOwner, MediaScope, ModelImage

    assert ModelMediaCapabilities().model_dump() == {
        "vision_enabled": False,
        "image_api_profile": "none",
        "audio_chat_enabled": False,
    }
    for invalid in (b"", b"x" * (MAX_MODEL_IMAGE_BYTES + 1)):
        with pytest.raises(ValidationError):
            ModelImage(media_type="image/png", data=invalid, source_size_bytes=MAX_IMAGE_BYTES)
    with pytest.raises(ValidationError):
        MediaOwner(entry="admin", session_id="admin_x", user_id="forged")
    with pytest.raises(ValidationError):
        MediaScope(
            owner=MediaOwner(entry="qq", session_id="qq"),
            execution_id="../escape",
            execution_kind="agent",
        )


def test_media_asset_requires_consistent_execution_and_safe_key():
    """存储键和执行归属不能由路径片段或半个执行标识构成。"""
    from agent_bot.media.contracts import MediaAssetRecord, MediaOwner

    values = dict(
        id="a" * 32,
        purpose="input",
        mime_type="image/png",
        size_bytes=20,
        sha256="b" * 64,
        status="writing",
        created_at=datetime(2026, 10, 12),
        expires_at=datetime(2026, 10, 12) + timedelta(days=1),
        owner=MediaOwner(entry="admin", session_id="admin_x"),
        storage_key="a" * 32,
    )
    assert MediaAssetRecord(**values).execution_id is None
    for changes in (
        {"storage_key": "../x"},
        {"execution_id": "c" * 32},
        {"expires_at": datetime(2020, 1, 1)},
    ):
        with pytest.raises(ValidationError):
            MediaAssetRecord(**{**values, **changes})


def test_media_bootstrap_environment_precedence_without_io(tmp_path, monkeypatch):
    """部署目录只在资源启动时创建，环境值覆盖不插值的文件设置。"""
    from agent_bot.media.config import MediaConfig

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AGENT_BOT_MEDIA_DIRECTORY=from-file\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_BOT_MEDIA_DIRECTORY", "from-env")
    assert MediaConfig.from_environment().directory == tmp_path / "from-env"
    assert not (tmp_path / "from-env").exists()
    monkeypatch.delenv("AGENT_BOT_MEDIA_DIRECTORY")
    assert MediaConfig.from_environment().directory == tmp_path / "from-file"
