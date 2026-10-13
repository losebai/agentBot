"""验证同会话并发、非等待准入和重置期间的资源释放。"""

import asyncio
from unittest.mock import AsyncMock

import pytest

from agent_bot.services.chat.errors import ChatBusyError
from agent_bot.services.chat.request_control import ChatRequestCoordinator


@pytest.mark.parametrize("reject_busy", [False, True])
async def test_same_session_uses_available_slots_without_waiting(reject_busy: bool) -> None:
    """同群请求共用历史标识，但不能共用执行互斥锁。"""
    coordinator = ChatRequestCoordinator(
        cooldown_seconds=0, max_concurrent_requests=2, max_rate_limit_entries=10,
    )
    async with coordinator.request("group", reject_busy=reject_busy, consume_cooldown=False):
        async with asyncio.timeout(0.1):
            async with coordinator.request(
                "group", reject_busy=reject_busy, consume_cooldown=False,
            ):
                with pytest.raises(ChatBusyError):
                    await coordinator.reset("group", AsyncMock())
        with pytest.raises(ChatBusyError):
            await coordinator.reset("group", AsyncMock())
    clear = AsyncMock(return_value=True)
    assert await coordinator.reset("group", clear)
    clear.assert_awaited_once()


@pytest.mark.parametrize("media", [False, True])
async def test_full_budget_rejects_queued_request_immediately(media: bool) -> None:
    """排队调用与直接调用都不能等待执行额度。"""
    coordinator = ChatRequestCoordinator(
        cooldown_seconds=0, max_concurrent_requests=1, max_rate_limit_entries=10,
    )
    async with coordinator.request("first", reject_busy=False, consume_cooldown=False, media=media):
        async with asyncio.timeout(0.1):
            with pytest.raises(ChatBusyError):
                async with coordinator.request(
                    "second", reject_busy=False, consume_cooldown=False, media=media,
                ):
                    pytest.fail("额度满时不得进入模型调用")


async def test_cancelled_request_releases_slot_and_session_activity() -> None:
    """取消后既能继续聊天，也能立即重置会话。"""
    coordinator = ChatRequestCoordinator(
        cooldown_seconds=0, max_concurrent_requests=1, max_rate_limit_entries=10,
    )
    entered = asyncio.Event()

    async def request() -> None:
        """持有唯一额度，供测试在调用途中取消。"""
        async with coordinator.request("s", reject_busy=False, consume_cooldown=False):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(request())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await coordinator.reset("s", AsyncMock(return_value=True))
    async with coordinator.request("s", reject_busy=False, consume_cooldown=False):
        pass


async def test_request_during_reset_is_rejected_without_waiting() -> None:
    """重置事务尚未结束时，新请求不能越过清理边界或等待清理锁。"""
    coordinator = ChatRequestCoordinator(
        cooldown_seconds=0, max_concurrent_requests=2, max_rate_limit_entries=10,
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def clear() -> bool:
        """暂停实际清理，暴露新请求的准入时机。"""
        entered.set()
        await release.wait()
        return True

    task = asyncio.create_task(coordinator.reset("s", clear))
    await entered.wait()
    try:
        async with asyncio.timeout(0.1):
            with pytest.raises(ChatBusyError):
                async with coordinator.request("s", reject_busy=False, consume_cooldown=False):
                    pytest.fail("重置期间不得生成回复")
    finally:
        release.set()
        await task
