"""在异步请求链中传递单调时钟总预算，嵌套调用只允许收紧。"""

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from math import isfinite
from time import monotonic

_deadline: ContextVar[float | None] = ContextVar("request_deadline", default=None)


@contextmanager
def request_deadline(seconds: float) -> Iterator[None]:
    """绑定当前请求的截止时间，并保留更早的父级截止时间。"""
    if not isfinite(seconds):
        raise ValueError("request timeout must be finite")
    deadline = monotonic() + seconds
    inherited = _deadline.get()
    token = _deadline.set(deadline if inherited is None else min(deadline, inherited))
    try:
        yield
    finally:
        _deadline.reset(token)


def remaining_timeout(maximum: float) -> float:
    """返回配置上限以内的剩余秒数，已过期时阻止后续网络或工具调用。"""
    deadline = _deadline.get()
    remaining = maximum if deadline is None else min(maximum, deadline - monotonic())
    if remaining <= 0:
        raise asyncio.TimeoutError("request deadline exceeded")
    return remaining


@contextmanager
def clear_request_deadline() -> Iterator[None]:
    """创建独立后台任务时显式清除请求上下文，退出后恢复原预算。"""
    token = _deadline.set(None)
    try:
        yield
    finally:
        _deadline.reset(token)
