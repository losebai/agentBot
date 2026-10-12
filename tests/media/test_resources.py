"""进程资源不在导入时初始化，缺表只影响媒体就绪。"""

import pytest

from agent_bot.media.config import MediaConfig
from agent_bot.media.errors import MediaError


async def test_resources_start_close_and_generation_slots(media_database, agent_params, tmp_path):
    """启动一次并维护全局两个名额，停止后不再接受请求。"""
    from agent_bot.media.resources import MediaResources

    resources = MediaResources(
        MediaConfig(directory=tmp_path / "media"), agent_params, connect=media_database.connect
    )
    assert not resources.config.directory.exists()
    await resources.start()
    assert resources.readiness.initialized and resources.readiness.storage_available
    async with resources.generations.slot(), resources.generations.slot():
        with pytest.raises(MediaError, match="MEDIA_BUSY"):
            async with resources.generations.slot():
                pass
    await resources.aclose()
    await resources.aclose()
    assert not resources.readiness.storage_available


async def test_missing_schema_does_not_prevent_text_start(agent_database, agent_params, tmp_path):
    """缺表不创建存储、不抛出启动异常；修复后需重启。"""
    from agent_bot.media.resources import MediaResources

    resources = MediaResources(
        MediaConfig(directory=tmp_path / "media"), agent_params, connect=agent_database.connect
    )
    await resources.start()
    assert not resources.readiness.initialized
    assert resources.readiness.code == "MEDIA_NOT_INITIALIZED"
    await resources.aclose()
