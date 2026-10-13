"""通过真实执行图验证轮数、批次边界和取消资源释放。"""

import asyncio
from types import SimpleNamespace

import pytest

from agent.test_model_protocols import model_call
from agent_bot.agent.contracts import (
    AgentRequest,
    ExecutionAuthority,
    ModelEvent,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent_bot.agent.engine import PreparedRun
from agent_bot.agent.tools.runtime import BoundTools


def prepared(rounds):
    """构造无需数据库的已授权运行快照。"""
    return PreparedRun(
        execution_id="run",
        request=AgentRequest(prompt="查资料", session_id="s", request_id="r", model="local"),
        snapshot=SimpleNamespace(skills=()),
        authority=ExecutionAuthority(entry="admin"),
        call=model_call("openai-chat"),
        max_steps=rounds,
    )


@pytest.mark.parametrize("rounds", [1, 3, 5])
async def test_graph_runs_rounds_then_one_final_call(rounds):
    """每轮八次工具调用，达到配置轮数后恰好进行一次收尾。"""
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools

    invoked = []
    model_calls = []

    class Provider:
        """模型始终要求工具，只有禁用工具时才输出最终答案。"""

        async def stream_turn(self, call):
            """为每轮生成唯一 ID，验证跨轮关联。"""
            model_calls.append(call)
            text = "最终答案" if call.final_only else "中间草稿"
            yield ModelEvent(kind="text_delta", delta=text)
            yield ModelEvent(
                kind="turn_end",
                turn=ModelTurn(
                    content=text,
                    model="actual",
                    calls=()
                    if call.final_only
                    else tuple(
                        ToolCall(call_id=f"{len(model_calls)}-{i}", name="lookup", arguments={})
                        for i in range(8)
                    ),
                ),
            )

    async def handle(call):
        """记录工具调用顺序，不执行外部操作。"""
        invoked.append(call.call_id)
        return ToolResult(call_id=call.call_id, name=call.name, content="facts")

    bound = BoundTools(
        (ToolDefinition(name="lookup", source="builtin", input_schema={"type": "object"}),),
        {"lookup": handle},
    )
    graph = build_agent_graph(Provider(), prepared(rounds), LangChainTools(bound))
    events = [
        event
        async for event in graph.astream(
            {}, stream_mode="custom", config={"recursion_limit": 4 * rounds + 4}
        )
    ]
    assert len(invoked) == rounds * 8
    assert len(model_calls) == rounds + 1 and model_calls[-1].final_only
    assert events[-1].kind == "done"
    assert [event.delta for event in events if event.kind == "text_delta"][-1] == "最终答案"


async def test_cancel_joins_graph_before_tools_close():
    """关闭图事件流会取消并等待正在运行的模型节点。"""
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools

    entered, closed = asyncio.Event(), asyncio.Event()

    class Provider:
        """模拟迟迟没有首段正文的上游。"""

        async def stream_turn(self, call):
            """取消时必须进入 finally 释放资源。"""
            entered.set()
            try:
                await asyncio.Event().wait()
                yield
            finally:
                closed.set()

    graph = build_agent_graph(Provider(), prepared(1), LangChainTools(BoundTools((), {})))
    stream = graph.astream({}, stream_mode="custom")
    assert (await anext(stream)).kind == "model_start"
    await asyncio.wait_for(entered.wait(), 1)
    await stream.aclose()
    assert closed.is_set()
