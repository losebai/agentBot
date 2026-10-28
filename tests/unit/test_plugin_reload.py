"""共享运行时热刷新与待重启插件选择的并发验证。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.unit.test_plugin_settings import valid_config

from agent_bot.core.application import ApplicationRuntime
from agent_bot.settings import DatabaseParams, VersionedSettings


def fake_state(config):
    """用可计数的关闭方法替代公共 HTTP 客户端。"""
    return SimpleNamespace(config=config, ai_client=AsyncMock(), comfy_client=None,
                           comfy_resources=None, tts_client=None)


def fake_runtime(monkeypatch):
    """配置读取与公共资源全部隔离，不连接任何数据库。"""
    config = valid_config()
    runtime = ApplicationRuntime(VersionedSettings(config, 1),
                                 database=DatabaseParams("localhost", 3306, "t", "t", "t"),
                                 database_pool=SimpleNamespace(connect=Mock(), aclose=AsyncMock()),
                                 media_resources=SimpleNamespace(aclose=AsyncMock()))
    runtime._state = fake_state(config)
    monkeypatch.setattr(runtime, "_build", fake_state)
    monkeypatch.setattr(runtime.settings_store, "load_revision", Mock(return_value=2))
    monkeypatch.setattr(runtime.settings_store, "load", Mock(return_value=VersionedSettings(
        valid_config(app_enabled_plugins=[], chat_system_prompt="新的提示"), 2,
    )))
    return runtime


async def test_disable_save_keeps_live_selection_and_applies_other_values(monkeypatch):
    """插件选择留到重启，其他运行值正常刷新并推进已处理版本。"""
    runtime = fake_runtime(monkeypatch)
    old = runtime.state
    assert await runtime.refresh_once()
    assert runtime.config.app_enabled_plugins == ["qqbot"]
    assert runtime.config.chat_system_prompt == "新的提示"
    assert runtime.revision == 2
    old.ai_client.aclose.assert_awaited_once()
    await runtime.stop()


async def test_refresh_waits_active_lease_and_does_not_close_used_clients(monkeypatch):
    """尚在使用的客户端须等待最后一个租约退出后才能释放。"""
    runtime = fake_runtime(monkeypatch)
    old = runtime.state
    async with runtime.lease():
        refresh = asyncio.create_task(runtime.refresh_once())
        await asyncio.sleep(0.03)
        assert not refresh.done()
        old.ai_client.aclose.assert_not_awaited()
        assert runtime.state is old
    await refresh
    old.ai_client.aclose.assert_awaited_once()
    await runtime.stop()


async def test_failed_channel_publish_restores_both_snapshots_and_resumes(monkeypatch):
    """渠道发布中途失败也不能遗留半个新快照或暂停的生产者。"""
    runtime = fake_runtime(monkeypatch)
    old = runtime.state
    participant = SimpleNamespace(state=object(), pause=AsyncMock(), resume=AsyncMock(),
                                  build=Mock(return_value=object()), close=AsyncMock())
    old_qq = participant.state

    def publish(state):
        """模拟发布部分属性后失败，再允许恢复旧状态。"""
        participant.state = state
        if state is not old_qq:
            raise RuntimeError("publish failed")

    participant.publish = publish
    runtime.bind_refresh_participant(participant)
    with pytest.raises(RuntimeError, match="publish failed"):
        await runtime.refresh_once()
    assert runtime.state is old
    assert participant.state is old_qq
    assert runtime.revision == 1
    old.ai_client.aclose.assert_not_awaited()
    participant.resume.assert_awaited_once()
    participant.close.assert_awaited_once_with(participant.build.return_value)
    await runtime.stop()
