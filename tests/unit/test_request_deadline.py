"""请求总预算不因嵌套调用或流式进度被重置。"""

import asyncio

import pytest

from agent_bot.services.request_deadline import (
    clear_request_deadline,
    remaining_timeout,
    request_deadline,
)


def test_nested_budget_cannot_extend_parent_and_clear_restores_it(monkeypatch) -> None:
    """嵌套请求只能缩短预算，后台任务可显式脱离继承的预算。"""
    clock = [100.0]
    monkeypatch.setattr("agent_bot.services.request_deadline.monotonic", lambda: clock[0])
    assert remaining_timeout(30) == 30
    with request_deadline(5):
        clock[0] += 2
        with request_deadline(60):
            assert remaining_timeout(30) == 3
        with clear_request_deadline():
            assert remaining_timeout(30) == 30
        assert remaining_timeout(30) == 3
        clock[0] += 4
        with pytest.raises(asyncio.TimeoutError):
            remaining_timeout(30)
    assert remaining_timeout(30) == 30
