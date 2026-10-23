"""通过真实执行图验证轮数、批次边界和取消资源释放。"""

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from agent.test_model_protocols import model_call
from agent_bot.agent.contracts import (
    AgentModelCall,
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


@pytest.mark.parametrize("name", ["image_generate", "local_workflow_generate_image"])
@pytest.mark.parametrize("same_batch", [False, True])
async def test_qq_queued_image_finishes_before_waiting_again(name: str, same_batch: bool) -> None:
    """绘图成功入队即结束父请求，同批或下一轮的等待都不能阻塞后台任务。"""
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools

    calls: list[AgentModelCall] = []

    class Provider:
        """首轮提交绘图；后续模型或工具等待代表原超时路径。"""

        async def stream_turn(self, call: AgentModelCall) -> AsyncIterator[ModelEvent]:
            """明确模拟同批等待和下一轮轮询，不访问真实模型。"""
            calls.append(call)
            arguments = {"start_reply": "我来画这张图。"}
            arguments.update({"prompt": "一只猫"} if name == "image_generate" else {
                "workflow_key": "cat", "positive_prompt": "a cat",
            })
            image = ToolCall(call_id="image", name=name, arguments=arguments)
            wait = ToolCall(call_id=f"wait-{len(calls)}", name="Wait", arguments={})
            tools = (image, wait) if same_batch else (image,)
            yield ModelEvent(kind="turn_end", turn=ModelTurn(
                model="actual", calls=tools if len(calls) == 1 else (wait,),
            ))

    async def handle(call: ToolCall) -> ToolResult:
        """后台提交已成功；执行等待就是此次回归要防止的超时。"""
        if call.name == "Wait":
            raise TimeoutError("remote Wait exceeded its 10 second limit")
        return ToolResult(call_id=call.call_id, name=call.name,
                          content='{"status":"queued","task_id":"image-task"}')

    bound = BoundTools((
        ToolDefinition(name=name, source="chat", input_schema={"type": "object"}),
        ToolDefinition(name="Wait", source="mcp", input_schema={"type": "object"}),
    ), {name: handle, "Wait": handle})
    run = prepared(5)
    run.authority = ExecutionAuthority(entry="qq")
    graph = build_agent_graph(Provider(), run, LangChainTools(bound))
    events = [event async for event in graph.astream({}, stream_mode="custom")]
    assert events[-1].kind == "done" and events[-1].metadata.model == "actual"
    assert len(calls) == 1
    assert [event.tool for event in events if event.kind == "tool_result"] == [name]
    assert any(event.kind == "text_delta" and event.delta.strip() for event in events)


@pytest.mark.parametrize("entry,source,content,is_error", [
    ("admin", "chat", '{"status":"queued","task_id":"task"}', False),
    ("qq", "mcp", '{"status":"queued","task_id":"task"}', False),
    ("qq", "chat", '{"status":"queued","task_id":"task"}', True),
    ("qq", "chat", '{"status":"queued"}', False),
    ("qq", "chat", '{"image_index":1}', False),
    ("qq", "chat", 'not a receipt', False),
])
async def test_only_successful_local_qq_image_receipt_ends_agent(
    entry: str, source: str, content: str, is_error: bool,
) -> None:
    """同步管理生成、远端伪造和失败结果仍需正常收尾，不能被当成已提交图片。"""
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools

    model_calls: list[AgentModelCall] = []

    class Provider:
        """单轮工具后正常收尾，以实际回答判断是否被错误提前结束。"""

        async def stream_turn(self, call: AgentModelCall) -> AsyncIterator[ModelEvent]:
            """只替换外部模型，工具及图的预检和条件边保持真实。"""
            model_calls.append(call)
            if len(model_calls) == 1:
                yield ModelEvent(kind="turn_end", turn=ModelTurn(model="actual", calls=(
                    ToolCall(call_id="image", name="image_generate", arguments={
                        "prompt": "一只猫", "start_reply": "开始画图。",
                    }),
                )))
            else:
                yield ModelEvent(kind="text_delta", delta="继续正常收尾")
                yield ModelEvent(kind="turn_end", turn=ModelTurn(
                    model="actual", content="继续正常收尾",
                ))

    async def handle(call: ToolCall) -> ToolResult:
        """返回待验证的边界结果，不提交图片或发送消息。"""
        return ToolResult(call_id=call.call_id, name=call.name, content=content, is_error=is_error)

    bound = BoundTools((ToolDefinition(
        name="image_generate", source=source, input_schema={"type": "object"},
    ),), {"image_generate": handle})
    run = prepared(1)
    run.authority = ExecutionAuthority(entry=entry)
    graph = build_agent_graph(Provider(), run, LangChainTools(bound))
    events = [event async for event in graph.astream({}, stream_mode="custom")]
    assert len(model_calls) == 2 and model_calls[-1].final_only
    assert [event.delta for event in events if event.kind == "text_delta"] == ["继续正常收尾"]
    assert events[-1].kind == "done"
