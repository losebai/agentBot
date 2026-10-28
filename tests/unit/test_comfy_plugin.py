"""Comfy 插件隔离、共享快照和生命周期使用离线替身验证。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.unit.test_plugin_settings import valid_config

from agent_bot.core.application import ApplicationRuntime
from agent_bot.settings import DatabaseParams, VersionedSettings


def make_runtime(**values):
    """禁止数据库和媒体网络访问，仅装配内存中的宿主。"""
    config = valid_config(chat_local_workflow_enabled=True, chat_mcp_enabled=True,
                          chat_image_generation_enabled=True, **values)
    runtime = ApplicationRuntime(
        VersionedSettings(config, 1),
        database=DatabaseParams("localhost", 3306, "test", "test", "test"),
        database_pool=SimpleNamespace(connect=Mock(side_effect=AssertionError("database")),
                                      aclose=AsyncMock()),
        media_resources=SimpleNamespace(start=AsyncMock(), aclose=AsyncMock()),
    )
    runtime.workflow_store = Mock()
    return runtime


async def test_disabled_comfy_does_not_construct_or_scan(monkeypatch):
    """旧业务开关不能绕过正式宿主的插件选择。"""
    from agent_bot.plugins.comfy import plugin

    client = Mock(side_effect=AssertionError("disabled client"))
    monkeypatch.setattr(plugin, "ComfyClient", client)
    scan = Mock(side_effect=AssertionError("disabled scan"))
    monkeypatch.setattr(plugin.WorkflowCatalogImporter, "scan", scan)
    runtime = make_runtime(app_enabled_plugins=[])
    # 媒体替身不参与实际公共服务装配。
    runtime.media_resources.materials = runtime.media_resources.store = None
    try:
        await runtime.start()
        assert runtime.state.comfy_client is None
        assert runtime.state.workflow_service is None
        from agent_bot.agent.tools.media import AdminMediaTools

        tools = AdminMediaTools(runtime.config, None, None, Mock(), image_service=Mock())
        assert 'image_generate' in tools.allowed_tools
        assert not any(name.startswith('local_workflow_') for name in tools.allowed_tools)
        runtime.workflow_store.list_all.assert_not_called()
    finally:
        await runtime.stop()
    client.assert_not_called()
    scan.assert_not_called()


async def test_comfy_prepare_start_and_stop_wait_for_lease(monkeypatch, tmp_path):
    """准备无副作用；启动不探测远端；退出等在途借用结束才关闭。"""
    from agent_bot.plugins.comfy import plugin

    client = AsyncMock()
    factory = Mock(return_value=client)
    monkeypatch.setattr(plugin, "ComfyClient", factory)
    scan = Mock(return_value=())
    monkeypatch.setattr(plugin.WorkflowCatalogImporter, "scan", scan)
    runtime = make_runtime(app_enabled_plugins=['comfy'],
                           chat_local_workflow_directory=str(tmp_path))
    comfy = plugin.ComfyPlugin()
    comfy.prepare(SimpleNamespace(runtime=runtime))
    factory.assert_not_called()
    resources = comfy.build_resources(runtime.config)
    assert resources.workflow_service._client is client
    await comfy.start()
    assert await comfy.is_ready()
    assert scan.call_args.args[0] == tmp_path
    async with runtime.lease():
        stop = asyncio.create_task(comfy.stop())
        await asyncio.sleep(0)
        client.aclose.assert_not_awaited()
    await stop
    await resources.aclose()
    await runtime.stop()
    client.aclose.assert_awaited_once()


def test_workflow_management_capability_requires_running_comfy():
    """工作流存储保留，但待重启启用不能提前开放 HTTP 管理能力。"""
    from agent_bot.admin.application_runtime import ApplicationRuntimeProvider

    store = object()
    plugins = SimpleNamespace(running_plugin=Mock(return_value=None))
    provider = ApplicationRuntimeProvider(SimpleNamespace(workflow_store=store), plugins)
    assert provider.workflow_store is None
    plugins.running_plugin.return_value = object()
    assert provider.workflow_store is store
    plugins.running_plugin.assert_called_with('comfy')


async def test_failed_core_build_closes_unpublished_comfy(monkeypatch):
    """插件 start 前公共候选构建失败，也不能留下 HTTP 客户端。"""
    from agent_bot.plugins.comfy import plugin

    client = AsyncMock()
    monkeypatch.setattr(plugin, "ComfyClient", Mock(return_value=client))
    runtime = make_runtime(app_enabled_plugins=['comfy'])
    comfy = plugin.ComfyPlugin()
    comfy.prepare(SimpleNamespace(runtime=runtime))
    monkeypatch.setattr(runtime, "_build", Mock(side_effect=ValueError("build")))
    with pytest.raises(ValueError, match="build"):
        await runtime.start()
    await runtime.stop()
    client.aclose.assert_awaited_once()


async def test_comfy_refresh_retires_only_after_active_request(monkeypatch):
    """地址热刷新与 QQ 借用同一代，保存停用选择仍留待重启。"""
    from agent_bot.core.runtime import close_core_state
    from agent_bot.plugins.comfy import plugin

    clients = []

    def create_client(**kwargs):
        """记录每个独立配置版本，不建立真实网络连接。"""
        client = AsyncMock()
        client.address = kwargs['base_url']
        clients.append(client)
        return client

    monkeypatch.setattr(plugin, 'ComfyClient', create_client)
    runtime = make_runtime(app_enabled_plugins=['comfy'])
    comfy = plugin.ComfyPlugin()
    comfy.prepare(SimpleNamespace(runtime=runtime))

    def build(config, *, comfy_resources):
        """保留生产关闭契约，只替换无关的公共 AI 服务。"""
        return SimpleNamespace(config=config, ai_client=AsyncMock(), tts_client=None,
                               comfy_client=comfy_resources.client,
                               workflow_service=comfy_resources.workflow_service,
                               comfy_resources=comfy_resources, comfy_owner=comfy)

    monkeypatch.setattr(runtime, '_build', build)
    await runtime.start()
    await comfy.start()
    old = runtime.state
    candidate = runtime.config.model_copy(update={
        'app_enabled_plugins': [], 'chat_local_workflow_base_url': 'http://localhost:9999',
    })
    async with runtime.lease():
        refresh = asyncio.create_task(runtime._apply_settings(VersionedSettings(candidate, 2)))
        await asyncio.sleep(0.01)
        assert runtime.state is old
        clients[0].aclose.assert_not_awaited()
    await refresh
    assert runtime.config.app_enabled_plugins == ['comfy']
    assert runtime.state.comfy_client.address == 'http://localhost:9999'
    clients[0].aclose.assert_awaited_once()
    # 失败的 QQ 发布只回收新候选，不关闭仍生效的 Comfy 代。
    current = runtime.state
    participant = SimpleNamespace(state=old, pause=AsyncMock(), resume=AsyncMock(),
                                  close=AsyncMock(), build=Mock(side_effect=ValueError('qq build')),
                                  publish=Mock())
    runtime.bind_refresh_participant(participant)
    with pytest.raises(ValueError, match='qq build'):
        await runtime._apply_settings(VersionedSettings(candidate.model_copy(update={
            'chat_local_workflow_base_url': 'http://localhost:9998',
        }), 3))
    assert runtime.state is current
    clients[1].aclose.assert_not_awaited()
    clients[2].aclose.assert_awaited_once()
    await comfy.stop()
    await close_core_state(current)
    await runtime.stop()
    assert all(client.aclose.await_count == 1 for client in clients)


async def test_business_flag_off_keeps_workflow_management_without_client(monkeypatch):
    """插件运行与执行开关独立，关闭执行仍能管理现有工作流。"""
    from agent_bot.plugins.comfy import plugin

    runtime = make_runtime(app_enabled_plugins=['comfy'])
    runtime._config = runtime.config.model_copy(update={'chat_local_workflow_enabled': False})
    factory = Mock(side_effect=AssertionError('execution disabled'))
    monkeypatch.setattr(plugin, 'ComfyClient', factory)
    comfy = plugin.ComfyPlugin()
    comfy.prepare(SimpleNamespace(runtime=runtime))
    resources = comfy.build_resources(runtime.config)
    await comfy.start()
    assert await comfy.is_ready()
    assert resources.client is resources.workflow_service is None
    runtime.workflow_store.list_all.assert_called_once_with(limit=1)
    await comfy.stop()
    await runtime.stop()


async def test_cancelled_resource_close_finishes_once(monkeypatch):
    """调用者重复取消时仍等待插件连接释放，并保留取消传播语义。"""
    from agent_bot.plugins.comfy import plugin

    entered, released = asyncio.Event(), asyncio.Event()

    async def close():
        """人为挂起清理，确认取消没有传给客户端关闭任务。"""
        entered.set()
        await released.wait()

    client = SimpleNamespace(aclose=AsyncMock(side_effect=close))
    monkeypatch.setattr(plugin, 'ComfyClient', Mock(return_value=client))
    runtime = make_runtime(app_enabled_plugins=['comfy'])
    resources = plugin.ComfyResources(runtime.config, runtime.workflow_store)
    task = asyncio.create_task(resources.aclose())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    released.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await resources.aclose()
    await runtime.stop()
    client.aclose.assert_awaited_once()
