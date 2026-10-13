"""在隔离的 NoneBot 生命周期中验证 Python 后台能力的装配。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from tests.jobs.test_integration import config

from agent_bot.config import RuntimeConfig
from agent_bot.integrations.onebot.ai_jobs import AiJobRunner
from agent_bot.settings import DatabaseParams


async def test_job_execution_holds_runtime_lease_and_observes_reloaded_switch():
    """热刷新等待在途执行，发布关闭配置后不再领取任务。"""
    from agent_bot.plugin.lifecycle import RuntimeLeaseGate

    gate, started, release = RuntimeLeaseGate(), asyncio.Event(), asyncio.Event()
    current = [config(chat_job_enabled=True, chat_mcp_enabled=True)]

    async def execute():
        """模拟已经开始的普通模型推理。"""
        started.set()
        await release.wait()

    client = SimpleNamespace(run_due_jobs=AsyncMock(side_effect=execute))
    runner = AiJobRunner(
        config_provider=lambda: current[0], client_provider=lambda: client, runtime_lease=gate.lease
    )
    execution = asyncio.create_task(runner.run_once())
    await started.wait()
    await gate.begin_refresh()
    idle = asyncio.create_task(gate.wait_until_idle())
    await asyncio.sleep(0)
    assert not idle.done()
    release.set()
    await execution
    await idle
    current[0] = config()
    await gate.end_refresh()
    await runner.run_once()
    client.run_due_jobs.assert_awaited_once()


async def test_runtime_wires_python_services_and_settings_metadata():
    """启用开关后运行时有实际服务，管理界面获得中文字段和脱敏元数据。"""
    from agent_bot.admin.settings import SettingsService
    from agent_bot.plugin.runtime import build_runtime_state, close_runtime_state
    from agent_bot.settings import VersionedSettings

    settings = RuntimeConfig(
        onebot_v11_access_token="o" * 32,
        chat_session_secret="s" * 32,
        chat_job_enabled=True,
        chat_mcp_enabled=True,
        chat_auto_speak_enabled=True,
        chat_auto_speak_bot_id="10001",
        chat_auto_speak_groups={"10003"},
        chat_auto_speak_mcp_code="decision",
        chat_group_recent_context_enabled=True,
    )
    state = build_runtime_state(
        settings,
        database=DatabaseParams("localhost", 3306, "offline", "offline", "offline"),
        database_pool=SimpleNamespace(
            connect=Mock(side_effect=AssertionError("no database access during wiring"))
        ),
        bot_provider=lambda _: None,
        auto_speak_error_handler=lambda _: None,
        auto_speak_job_submitter=AsyncMock(),
        auto_speak_tools_provider=lambda: (),
    )
    try:
        assert state.ai_client.job_service.ai_service is state.ai_client.python_service
        assert state.auto_speak_service is not None
        assert state.auto_speak_service._decision_client.mcp_code == "decision"
        assert (await state.auto_speak_service.run_once()).bot_available is False
        metadata = SettingsService(None, SimpleNamespace(config=settings, revision=1))
        result = metadata._snapshot(VersionedSettings(config=settings, revision=1))
        fields = {field.name: field for field in result.fields}
        assert fields["chat_auto_speak_mcp_code"].value == "decision"
        assert fields["chat_auto_speak_bot_id"].value is None
        assert fields["chat_job_enabled"].kind == "boolean"
        assert "Python" in fields["chat_job_enabled"].description
    finally:
        await close_runtime_state(state)
