"""QQBot 生命周期适配只管理渠道资源的离线测试。"""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.unit.test_plugin_settings import valid_config


async def test_memory_maintenance_sleeps_and_review_survives_group_cleanup_failure(
    chat_plugin, monkeypatch,
):
    """群临时背景故障不阻断事实复盘，每轮维护前休眠且不调用模型。"""
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(chat_plugin.asyncio, "sleep", sleep)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_tagged_memory_enabled", True)
    group = Mock(cleanup_expired=Mock(side_effect=chat_plugin.ChatMemoryStoreError("offline")))
    store = Mock(expire_due=Mock(return_value=0), record_daily_review=Mock(return_value={
        "date": "2026-11-04", "status_counts": {}, "conflict_count": 0,
        "expiring_count": 0, "stale_candidates": [], "revision_count": 0,
    }))
    monkeypatch.setattr(chat_plugin, "group_recent_context_store", group)
    monkeypatch.setattr(chat_plugin, "_tagged_memory_store", store)
    with pytest.raises(asyncio.CancelledError):
        await chat_plugin._memory_cleanup_loop()
    assert sleep.await_count == 2
    assert all(call.args == (3600,) for call in sleep.await_args_list)
    store.expire_due.assert_called_once_with(limit=500)
    store.record_daily_review.assert_called_once_with()


async def test_memory_maintenance_cancellation_waits_for_database_thread(chat_plugin, monkeypatch):
    """取消后台循环必须等已启动的数据库工作收尾，再允许释放宿主资源。"""
    real_sleep = asyncio.sleep
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def expire_due(*, limit):
        """模拟持有数据库连接的工作，主循环取消不能提前结束它。"""
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return 0

    monkeypatch.setattr(chat_plugin.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_tagged_memory_enabled", True)
    monkeypatch.setattr(chat_plugin, "group_recent_context_store",
                        Mock(cleanup_expired=Mock(return_value=0)))
    store = Mock(expire_due=expire_due)
    monkeypatch.setattr(chat_plugin, "_tagged_memory_store", store)
    task = asyncio.create_task(chat_plugin._memory_cleanup_loop())
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        task.cancel()
        await real_sleep(0)
        assert not task.done()
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    store.record_daily_review.assert_not_called()


async def test_qqbot_prepare_registers_adapter_but_start_owns_queue(monkeypatch):
    """同步准备无队列副作用，部分启动失败也能执行渠道清理。"""
    from agent_bot.plugins.qqbot.plugin import QQBotPlugin

    events = []
    module = SimpleNamespace(
        initialize_channel_runtime=Mock(),
        mark_channel_ready=Mock(),
        initialize_chat_memory=AsyncMock(),
        start_memory_cleanup=AsyncMock(),
        start_conversation_queue=AsyncMock(),
        start_chat_mcp=AsyncMock(),
        start_auto_speak=AsyncMock(),
        start_ai_job_polling=AsyncMock(),
        close_clients=AsyncMock(),
        check_management_readiness=AsyncMock(return_value=True),
    )
    driver = SimpleNamespace(register_adapter=Mock(side_effect=lambda _: events.append("adapter")))
    runtime = SimpleNamespace(
        config=valid_config(), bind_chat_bridge=Mock(), bind_refresh_participant=Mock()
    )
    plugin = QQBotPlugin()
    monkeypatch.setattr(plugin, "_load_module", lambda: module)
    plugin.prepare(SimpleNamespace(driver=driver, runtime=runtime, app=None))
    assert events == ["adapter"]
    module.start_conversation_queue.assert_not_awaited()
    await plugin.start()
    assert await plugin.is_ready()
    module.start_conversation_queue.assert_awaited_once()
    await plugin.stop()
    await plugin.stop()
    module.close_clients.assert_awaited_once()


def test_disabled_application_import_has_no_message_plugin_or_queue():
    """独立进程避免 NoneBug 全局插件夹具掩盖核心导入耦合。"""
    import subprocess
    import sys

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "import agent_bot.core.application\n"
                "import agent_bot.plugins.catalog\n"
                "assert 'agent_bot.plugin.application' not in sys.modules\n"
                "assert 'agent_bot.integrations.onebot.ai_jobs' not in sys.modules\n"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
