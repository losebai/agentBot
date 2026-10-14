"""QQBot 生命周期适配只管理渠道资源的离线测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests.unit.test_plugin_settings import valid_config


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
