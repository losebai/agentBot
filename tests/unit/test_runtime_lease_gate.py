"""热刷新期间管理读取须快速失败，已受理业务仍受原有排空与取消规则保护。"""

import asyncio
from types import SimpleNamespace

import pytest

from agent_bot.admin.application_runtime import ApplicationRuntimeProvider
from agent_bot.core.lifecycle import RuntimeLeaseGate


async def test_management_request_does_not_wait_for_inflight_model_during_refresh() -> None:
    """真实管理提供器不能让记录读取跟随长模型请求一直等待热更新。"""
    from agent_bot.core.lifecycle import RuntimeRefreshInProgressError

    gate = RuntimeLeaseGate()
    provider = ApplicationRuntimeProvider(SimpleNamespace(lease=gate.lease), None)

    async def management() -> None:
        """模拟进入管理记录读取，防止回归时测试本身挂在门闩上。"""
        async with provider.lease():
            pytest.fail("刷新未结束不能借用即将关闭的快照")

    async with gate.lease():
        await gate.begin_refresh()
        with pytest.raises(RuntimeRefreshInProgressError):
            await asyncio.wait_for(management(), timeout=1)
    await asyncio.wait_for(gate.wait_until_idle(), timeout=1)
    await gate.end_refresh()
    async with provider.lease():
        pass


async def test_business_request_still_waits_and_cancelled_waiter_does_not_leak() -> None:
    """管理快速失败不能改变普通业务等待刷新和取消后归还名额的行为。"""
    gate = RuntimeLeaseGate()
    entered = asyncio.Event()

    async def business() -> None:
        """标记真正获得租约的业务任务。"""
        async with gate.lease():
            entered.set()

    await gate.begin_refresh()
    cancelled = asyncio.create_task(business())
    await asyncio.sleep(0)
    assert not entered.is_set()
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    waiting = asyncio.create_task(business())
    await asyncio.sleep(0)
    assert not entered.is_set()
    await gate.end_refresh()
    await asyncio.wait_for(waiting, timeout=1)
    assert entered.is_set()
    await asyncio.wait_for(gate.wait_until_idle(), timeout=1)
