"""通过任务局部上下文标记显式指令调用，不改变模型协议或请求关联标识。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar

command_call: ContextVar[bool] = ContextVar("command_call", default=False)


@asynccontextmanager
async def command_scope() -> AsyncIterator[None]:
    """指令内创建的模型任务继承标记，退出或取消后恢复调用方的上下文。"""
    token = command_call.set(True)
    try:
        yield
    finally:
        command_call.reset(token)
