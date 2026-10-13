"""运行时刷新共享媒体资源，同时替换已经退役的客户端。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_bot.config import RuntimeConfig
from agent_bot.media.config import MediaConfig


async def test_runtime_owns_clients_but_shares_media_resources(
    media_database, agent_params, tmp_path
):
    """构造不创建目录；两次快照引用同一存储，不关闭彼此的 HTTP 客户端。"""
    from agent_bot.media.resources import MediaResources
    from agent_bot.plugin.runtime import build_runtime_state, close_runtime_state

    resources = MediaResources(
        MediaConfig(directory=tmp_path / "media"), agent_params, connect=media_database.connect
    )
    config = RuntimeConfig(
        onebot_v11_access_token="x" * 40,
        chat_session_secret="y" * 40,
        chat_auto_speak_enabled=False,
    )
    kwargs = dict(
        database=agent_params,
        database_pool=SimpleNamespace(connect=media_database.connect),
        bot_provider=lambda _: None,
        auto_speak_error_handler=lambda _: None,
        auto_speak_job_submitter=AsyncMock(),
        auto_speak_tools_provider=lambda: (),
        media_resources=resources,
    )
    one = build_runtime_state(config, **kwargs)
    two = build_runtime_state(config, previous=one, **kwargs)
    try:
        assert not resources.config.directory.exists()
        assert (
            one.ai_client.media_service.resources
            is two.ai_client.media_service.resources
            is resources
        )
        assert one.ai_client.agent_service.media_store is resources.store
        assert one.ai_client.media_service.provider is not two.ai_client.media_service.provider
        await resources.start()
        await close_runtime_state(one)
        assert resources.readiness.storage_available
        assert not two.ai_client.media_service.provider.transport.http.is_closed
    finally:
        await close_runtime_state(two)
        await resources.aclose()
