"""通过任务局部上下文标记指令及自动发言系统调用，保持普通模型协议不变。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

command_call: ContextVar[bool] = ContextVar("command_call", default=False)
auto_speak_call: ContextVar[bool] = ContextVar("auto_speak_call", default=False)


@asynccontextmanager
async def auto_speak_scope() -> AsyncIterator[None]:
    """独立模型的单次群聊决策使用系统分类，退出或取消时恢复标记。"""
    token = auto_speak_call.set(True)
    try:
        yield
    finally:
        auto_speak_call.reset(token)


@asynccontextmanager
async def command_scope() -> AsyncIterator[None]:
    """指令内创建的模型任务继承标记，退出或取消后恢复调用方的上下文。"""
    token = command_call.set(True)
    try:
        yield
    finally:
        command_call.reset(token)
