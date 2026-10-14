"""应用插件生命周期、失败回收和待重启状态的离线测试。"""

import asyncio
from types import SimpleNamespace

import pytest


class FakePlugin:
    """用事件记录验证生命周期，不创建平台或网络资源。"""

    def __init__(self, metadata, events, *, fail=None):
        """保存插件身份和故障阶段。"""
        self.metadata, self.events, self.fail = metadata, events, fail

    def prepare(self, context):
        """记录同步准备。"""
        self.events.append(f"prepare:{self.metadata.id}")

    async def start(self):
        """允许在部分启动后失败或取消。"""
        self.events.append(f"start:{self.metadata.id}")
        if self.fail == "start":
            raise RuntimeError("secret upstream detail")
        if self.fail == "cancel":
            raise asyncio.CancelledError

    async def stop(self):
        """记录释放，即使故障也证明后续资源得到清理。"""
        self.events.append(f"stop:{self.metadata.id}")
        if self.fail == "stop":
            raise RuntimeError("secret shutdown detail")

    async def is_ready(self):
        """测试插件自身健康。"""
        return True

    def bots(self):
        """测试不创建机器人。"""
        return []


def make_manager(*, enabled=("first", "second"), failures=None):
    """构造注册顺序与用户选择顺序独立的管理器。"""
    from agent_bot.plugins.contracts import BuiltinPlugin, PluginMetadata
    from agent_bot.plugins.manager import PluginManager

    events = []
    failures = failures or {}
    catalog = []
    for name in ("first", "second"):
        metadata = PluginMetadata(name, name, "1", "测试插件")
        catalog.append(
            BuiltinPlugin(
                metadata,
                lambda meta=metadata: FakePlugin(
                    meta,
                    events,
                    fail=failures.get(meta.id),
                ),
            )
        )
    manager = PluginManager(catalog, enabled)
    manager.prepare(SimpleNamespace())
    return manager, events


async def test_plugins_start_in_registration_order_and_stop_in_reverse():
    """配置顺序不改变注册顺序，重复退出不重复释放。"""
    manager, events = make_manager(enabled=("second", "first"))
    await manager.start()
    assert await manager.is_ready()
    await manager.stop()
    await manager.stop()
    assert events == [
        "prepare:first",
        "prepare:second",
        "start:first",
        "start:second",
        "stop:second",
        "stop:first",
    ]


@pytest.mark.parametrize("enabled", [("missing",), ("first", "first")])
def test_unknown_or_duplicate_plugin_is_rejected(enabled):
    """错误选择在调用任何插件工厂前拒绝。"""
    with pytest.raises(ValueError):
        make_manager(enabled=enabled)


@pytest.mark.parametrize("failure", ["start", "cancel"])
async def test_failed_or_cancelled_start_cleans_partial_and_previous_plugins(failure):
    """启动中的插件和已启动插件都必须回收。"""
    from agent_bot.plugins.manager import PluginLifecycleError

    manager, events = make_manager(failures={"second": failure})
    with pytest.raises(asyncio.CancelledError if failure == "cancel" else PluginLifecycleError):
        await manager.start()
    assert events[-2:] == ["stop:second", "stop:first"]
    assert not await manager.is_ready()


async def test_shutdown_failure_continues_other_cleanup_and_redacts_error():
    """一次资源释放失败不能阻止其他插件退出。"""
    from agent_bot.plugins.manager import PluginLifecycleError

    manager, events = make_manager(failures={"second": "stop"})
    await manager.start()
    with pytest.raises(PluginLifecycleError) as caught:
        await manager.stop()
    assert events[-2:] == ["stop:second", "stop:first"]
    assert "secret" not in str(caught.value)
    import traceback

    assert "secret" not in "".join(traceback.format_exception(caught.value))


async def test_disabled_factory_is_not_called_and_save_does_not_change_live_state():
    """保存列表只改变待重启展示，不启动或卸载插件。"""
    manager, events = make_manager(enabled=("first",))
    await manager.start()
    snapshots = manager.snapshots([])
    assert snapshots[0].enabled and not snapshots[0].saved_enabled
    assert snapshots[0].restart_required and snapshots[0].status == "running"
    assert snapshots[1].status == "disabled"
    assert all("second" not in event for event in events)
