"""公共服务独立于 QQ 平台的资源与租约测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.unit.test_plugin_settings import valid_config

from agent_bot.settings import DatabaseParams, VersionedSettings


async def test_shared_services_can_be_built_without_qq_callbacks_or_queue():
    """公共快照只装配公共服务，无需 QQ 对话或机器人提供器。"""
    from agent_bot.core.runtime import build_core_state, close_core_state

    config = valid_config(app_enabled_plugins=[])
    state = build_core_state(
        config,
        database=DatabaseParams("localhost", 3306, "test", "test", "test"),
        database_pool=SimpleNamespace(connect=Mock(side_effect=AssertionError("no database"))),
    )
    try:
        assert state.ai_client.python_service is not None
        assert state.ai_client.agent_service is not None
        assert state.ai_client.agent_service.tool_runtime.chat_bridge is None
    finally:
        await close_core_state(state)


async def test_core_close_failure_still_closes_other_clients():
    """首个客户端失败时仍释放所有其他公共资源。"""
    from agent_bot.core.runtime import close_core_state

    clients = [AsyncMock(), AsyncMock(), AsyncMock()]
    clients[0].aclose.side_effect = RuntimeError("client failure")
    state = SimpleNamespace(ai_client=clients[0], comfy_client=clients[1], tts_client=clients[2])
    with pytest.raises(RuntimeError):
        await close_core_state(state)
    for client in clients:
        client.aclose.assert_awaited_once()


async def test_core_constructor_does_not_build_http_clients_and_pool_closes_last(monkeypatch):
    """启动前不创建 HTTP 快照，释放顺序保留媒体和数据库所有权。"""
    from agent_bot.core import application

    events = []

    def record(name):
        """记录每项退出顺序。"""
        events.append(name)

    pool = SimpleNamespace(connect=Mock(), aclose=AsyncMock(side_effect=lambda: record("pool")))
    media = SimpleNamespace(
        start=AsyncMock(), aclose=AsyncMock(side_effect=lambda: record("media"))
    )
    build = Mock()
    monkeypatch.setattr(application, "build_core_state", build)
    runtime = application.ApplicationRuntime(
        VersionedSettings(valid_config(app_enabled_plugins=[]), 1),
        database=DatabaseParams("localhost", 3306, "test", "test", "test"),
        database_pool=pool,
        media_resources=media,
    )
    build.assert_not_called()
    await runtime.stop()
    await runtime.stop()
    assert events == ["media", "pool"]


async def test_local_workflow_import_receives_path_and_runs_without_qq(monkeypatch, tmp_path):
    """工作流目录是启动字符串，公共导入边界须保持既有 Path 契约。"""
    from pathlib import Path

    from agent_bot.core import application

    config = valid_config(
        app_enabled_plugins=[],
        chat_local_workflow_enabled=True,
        chat_mcp_enabled=True,
        chat_image_generation_enabled=True,
        chat_local_workflow_directory=str(tmp_path),
    )
    runtime = application.ApplicationRuntime(
        VersionedSettings(config, 1),
        database=DatabaseParams("localhost", 3306, "t", "t", "t"),
        database_pool=SimpleNamespace(connect=Mock(), aclose=AsyncMock()),
        media_resources=SimpleNamespace(start=AsyncMock(), aclose=AsyncMock()),
    )
    runtime.workflow_store = Mock()
    runtime.workflow_store.prepare_for_startup.side_effect = AssertionError(
        "startup must not upgrade"
    )
    monkeypatch.setattr(
        runtime,
        "_build",
        lambda _: SimpleNamespace(
            config=config,
            ai_client=AsyncMock(),
            comfy_client=None,
            tts_client=None,
        ),
    )
    scan = Mock(return_value=())
    monkeypatch.setattr(application.WorkflowCatalogImporter, "scan", scan)
    try:
        await runtime.start()
        assert scan.call_args.args[0] == Path(tmp_path)
    finally:
        await runtime.stop()


async def test_cancelled_shutdown_finishes_draining_and_releases_all_resources(monkeypatch):
    """关闭取消须等待活跃租约退出后完成释放，重复关闭不能遗留资源。"""
    import asyncio

    from agent_bot.core import application

    clients = [AsyncMock(), AsyncMock(), AsyncMock()]
    runtime = application.ApplicationRuntime(
        VersionedSettings(valid_config(app_enabled_plugins=[]), 1),
        database=DatabaseParams("localhost", 3306, "t", "t", "t"),
        database_pool=SimpleNamespace(connect=Mock(), aclose=clients[2]),
        media_resources=SimpleNamespace(aclose=clients[1]),
    )
    runtime._state = SimpleNamespace(
        config=runtime.config,
        ai_client=SimpleNamespace(aclose=clients[0]),
        comfy_client=None,
        tts_client=None,
    )
    async with runtime.lease():
        stop = asyncio.create_task(runtime.stop())
        await asyncio.sleep(0)
        stop.cancel()
        await asyncio.sleep(0)
        assert not stop.done()
        assert all(client.await_count == 0 for client in clients)
    with pytest.raises(asyncio.CancelledError):
        await stop
    await runtime.stop()
    assert [client.await_count for client in clients] == [1, 1, 1]
