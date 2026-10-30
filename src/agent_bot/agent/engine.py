"""将 LangGraph 事件流接入管理 API 和 QQ 共用的执行服务。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING

import anyio
from langsmith import tracing_context

from agent_bot.agent.contracts import (
    AgentEvent,
    AgentModelCall,
    AgentRequest,
    AgentSnapshot,
    ExecutionAuthority,
)
from agent_bot.agent.graph import build_agent_graph
from agent_bot.agent.tools.langchain import LangChainTools
from agent_bot.agent.tools.runtime import BoundTools
from agent_bot.media.contracts import MediaScope, ValidatedImage

if TYPE_CHECKING:
    from agent_bot.agent.model_routing import AgentModelRouter
    from agent_bot.ai.providers.langchain import LangChainProvider


@dataclass
class PreparedRun:
    """预检后的不可变调用参数及整个执行持有的图片作用域。"""

    execution_id: str
    request: AgentRequest
    snapshot: AgentSnapshot
    authority: ExecutionAuthority
    call: AgentModelCall
    # 沿用内部字段名，含义为工具调用总次数；None 表示不限制次数。
    max_steps: int | None
    media_scope: MediaScope | None = None
    images: tuple[ValidatedImage, ...] = ()
    model_router: AgentModelRouter | None = None
    # 固定表达建议独立于管理员角色预算，仍受图中的总提示预算约束。
    evolution_prompt: str = ''


class AgentEngine:
    """持有请求级图执行，关闭流时等待节点取消和资源退出。"""

    def __init__(self, provider: LangChainProvider) -> None:
        """模型端口使用 LangChain，测试可注入受控异步模型。"""
        self.provider = provider

    async def run(self, prepared: PreparedRun, tools: BoundTools) -> AsyncIterator[AgentEvent]:
        """只输出项目事件，完整图状态不会进入 SSE 或外部 tracing。"""
        acknowledged = asyncio.Event()
        graph = build_agent_graph(
            self.provider, prepared, LangChainTools(tools), acknowledged=acknowledged
        )
        events: asyncio.Queue[AgentEvent | Exception | None] = asyncio.Queue(maxsize=1)

        async def produce() -> None:
            """同一任务拥有整个图生命周期，隔离 ASGI 重复取消后再显式取消一次。"""
            with anyio.CancelScope(shield=True), tracing_context(enabled=False):
                try:
                    async with aclosing(
                        graph.astream(
                            {},
                            stream_mode="custom",
                            # 不让框架默认递归次数形成隐藏预算；超时与取消仍由服务管理。
                            config={"recursion_limit": float("inf")},
                        )
                    ) as stream:
                        async for event in stream:
                            await events.put(event)
                except Exception as error:
                    await events.put(error)
                else:
                    await events.put(None)

        task = asyncio.create_task(produce())
        try:
            while True:
                event = await events.get()
                if event is None:
                    break
                if isinstance(event, Exception):
                    raise event
                yield event
                acknowledged.set()
        finally:
            # 图调度仍由 LangGraph 完成；此任务只负责流的背压和关闭所有权。
            with anyio.CancelScope(shield=True):
                if not task.done():
                    task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
