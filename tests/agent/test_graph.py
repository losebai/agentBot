"""通过真实执行图验证调用次数预算、批次边界和取消资源释放。"""

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

_DSML = "\uff5c\uff5cDSML\uff5c\uff5c"


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


def test_protocol_conversion_accepts_more_than_eight_calls():
    """实际 LangChain 协议转换不能在执行预算检查之前拒绝较大的调用批次。"""
    from langchain_core.messages import AIMessage

    from agent_bot.ai.providers.langchain_messages import to_model_turn

    message = AIMessage(content="", tool_calls=[
        {"id": f"call-{i}", "name": "lookup", "args": {}} for i in range(12)
    ])
    turn = to_model_turn(message, model_call("openai-chat"))
    assert len(turn.calls) == 12


@pytest.mark.parametrize("rounds", [1, 3, 5, 50])
async def test_graph_runs_configured_calls_then_one_final_call(rounds):
    """按实际调用次数收尾，即使模型批量请求也不能超出配置预算。"""
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
    assert len(invoked) == rounds
    assert len(model_calls) == (rounds + 7) // 8 + 1 and model_calls[-1].final_only
    assert events[-1].kind == "done"
    assert [event.delta for event in events if event.kind == "text_delta"][-1] == "最终答案"


@pytest.mark.parametrize("maximum", [120_000, 300_000])
async def test_accumulated_tool_results_use_configured_context_budget(maximum):
    """工具结果累积超过旧上限时，后续模型轮次仍按当前配置校验且不重放工具。"""
    from agent_bot.agent.errors import AgentError
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools

    invoked, model_calls = [], []

    class Provider:
        """首轮查询四项资料，工具额度用尽后根据已有结果收尾。"""

        async def stream_turn(self, call):
            """记录模型轮次，确认超限检查发生在调用上游之前。"""
            model_calls.append(call)
            if call.final_only:
                yield ModelEvent(kind="text_delta", delta="完成")
                yield ModelEvent(kind="turn_end", turn=ModelTurn(content="完成", model="actual"))
            else:
                yield ModelEvent(kind="turn_end", turn=ModelTurn(model="actual", calls=tuple(
                    ToolCall(call_id=f"lookup-{i}", name="lookup", arguments={})
                    for i in range(4)
                )))

    async def handle(call):
        """生成多项工具结果，验证总预算按所有结果的累计内容检查。"""
        invoked.append(call.call_id)
        return ToolResult(call_id=call.call_id, name=call.name, content="x" * 32_000)

    bound = BoundTools(
        (ToolDefinition(name="lookup", source="builtin", input_schema={"type": "object"}),),
        {"lookup": handle},
    )
    run = prepared(4)
    run.maximum_context_chars = maximum
    graph = build_agent_graph(Provider(), run, LangChainTools(bound))
    events = []
    if maximum == 120_000:
        with pytest.raises(AgentError, match="AGENT_CONTEXT_TOO_LARGE"):
            async for event in graph.astream({}, stream_mode="custom"):
                events.append(event)
        assert len(model_calls) == 1
    else:
        events = [event async for event in graph.astream({}, stream_mode="custom")]
        assert len(model_calls) == 2 and model_calls[-1].final_only
        assert events[-1].kind == "done"
    assert invoked == [f"lookup-{i}" for i in range(4)]


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


@pytest.mark.parametrize("has_tools", [False, True])
async def test_model_markup_pollution_is_corrected_once(has_tools):
    """纠正保留原工具权限，通知或额度耗尽时的收尾轮不得重新开启工具。"""
    from agent_bot.agent.errors import AgentError
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools

    attempts: list[str] = []

    class Provider:
        """第一次把工具调用写成正文标记，收到纠正提示后正常收尾。"""

        async def stream_turn(self, call):
            """依据真实可用工具检查纠正提示，避免收尾时鼓励工具执行。"""
            assert call.final_only is not has_tools
            assert bool(call.tools) is has_tools
            attempts.append(call.messages[-1].text)
            if len(attempts) == 1:
                polluted = (
                    "先看文档。"
                    f"<{_DSML} calls>"
                    f'<{_DSML} invoke name="read_url">'
                    f"</{_DSML} invoke></{_DSML} calls>"
                )
                yield ModelEvent(kind="text_delta", delta=polluted)
                raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            yield ModelEvent(kind="text_delta", delta="干净答案")
            yield ModelEvent(kind="turn_end", turn=ModelTurn(
                model="actual", content="干净答案",
            ))

    bound = BoundTools((
        ToolDefinition(name="read_url", source="builtin", input_schema={"type": "object"}),
    ), {})
    if not has_tools:
        bound = BoundTools((), {})
    graph = build_agent_graph(Provider(), prepared(1), LangChainTools(bound))
    events = [event async for event in graph.astream({}, stream_mode="custom")]
    assert len(attempts) == 2
    assert "[系统提示]" in attempts[1] and "DSML" in attempts[1]
    assert ("权限和剩余额度保持不变" in attempts[1]) is has_tools
    assert [event.code for event in events if event.kind == "warning"] == [
        "AGENT_MODEL_MARKUP_RETRIED"
    ]
    assert [event.delta for event in events if event.kind == "text_delta"][-1] == "干净答案"
    assert events[-1].kind == "done"


async def test_markup_correction_keeps_prior_results_and_executes_only_new_calls():
    """工具执行后的污染只重问当前模型轮，旧结果保留且工具副作用不重放。"""
    from agent_bot.agent.errors import AgentError
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools

    attempts, invoked = [], []

    class Provider:
        """先执行一次工具，再产生污染，纠正后继续执行剩余调用。"""

        async def stream_turn(self, call):
            """检查纠正后仍可使用工具，原工具结果和用户目标保持有效。"""
            attempts.append(call)
            number = len(attempts)
            if number == 2:
                yield ModelEvent(kind="text_delta", delta=f"继续<{_DSML} calls>未执行")
                raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            if number == 3:
                assert call.tools and not call.final_only
                results = [m.tool_result for m in call.messages if m.tool_result]
                assert [r.call_id for r in results] == ["first"]
                assert any(m.text == "lookup" for m in call.messages)
                assert not any("未执行" in m.text for m in call.messages[:-1])
            if number in {1, 3}:
                yield ModelEvent(kind="turn_end", turn=ModelTurn(model="actual", calls=(
                    ToolCall(call_id="first" if number == 1 else "second",
                             name="lookup", arguments={}),
                )))
            else:
                assert call.final_only and not call.tools
                yield ModelEvent(kind="text_delta", delta="已根据两次查询完成")
                yield ModelEvent(kind="turn_end", turn=ModelTurn(
                    model="actual", content="已根据两次查询完成",
                ))

    async def handle(call):
        """记录真实副作用次数，防止通过重跑整段会话恢复工具调用。"""
        invoked.append(call.call_id)
        return ToolResult(call_id=call.call_id, name=call.name, content="已核实资料")

    bound = BoundTools((ToolDefinition(
        name="lookup", source="builtin", input_schema={"type": "object"},
    ),), {"lookup": handle})
    graph = build_agent_graph(Provider(), prepared(2), LangChainTools(bound))
    events = [e async for e in graph.astream({}, stream_mode="custom")]
    assert invoked == ["first", "second"] and len(attempts) == 4
    assert [e.turn for e in events if e.kind == "model_start"] == [1, 2, 2, 3]
    assert events[-1].kind == "done"


async def test_markup_correction_cannot_extend_expired_deadline(monkeypatch):
    """纠正使用原总期限，到期后不得发出新的模型请求。"""
    from agent_bot.agent.errors import AgentError
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools
    from agent_bot.services.request_deadline import request_deadline

    now, attempts = [0.0], []
    monkeypatch.setattr("agent_bot.services.request_deadline.monotonic", lambda: now[0])

    class Provider:
        """输出异常时耗尽总期限，不使用真实等待或网络。"""

        async def stream_turn(self, call):
            """模型尝试仅记录一次，随后纠正必须服从原期限。"""
            attempts.append(call)
            yield ModelEvent(kind="text_delta", delta=f"<{_DSML} calls>")
            now[0] = 31.0
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")

    graph = build_agent_graph(Provider(), prepared(1), LangChainTools(BoundTools((), {})))
    with request_deadline(30), pytest.raises(asyncio.TimeoutError):
        [e async for e in graph.astream({}, stream_mode="custom")]
    assert len(attempts) == 1


async def test_model_markup_pollution_fails_after_one_retry():
    """纠正后仍输出伪工具标记则维持原有硬失败，不会无限重问。"""
    from agent_bot.agent.errors import AgentError
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools

    attempts: list[str] = []

    class Provider:
        """两次都把工具调用写进正文，应只重问一次后失败。"""

        async def stream_turn(self, call):
            attempts.append(call.messages[-1].text)
            yield ModelEvent(kind="text_delta", delta=f"<{_DSML} calls>再试</{_DSML} calls>")
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")

    bound = BoundTools((
        ToolDefinition(name="read_url", source="builtin", input_schema={"type": "object"}),
    ), {})
    graph = build_agent_graph(Provider(), prepared(1), LangChainTools(bound))
    with pytest.raises(AgentError, match="AGENT_MODEL_RESPONSE_INVALID"):
        [event async for event in graph.astream({}, stream_mode="custom")]
    assert len(attempts) == 2


async def test_model_markup_pollution_with_plain_prefix_still_fails_after_retry():
    """纠正无效时，即使有进度正文也不能把未完成的工具任务归档为成功。"""
    from agent_bot.agent.errors import AgentError
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools

    attempts: list[str] = []

    class Provider:
        """两次均输出进度和伪调用，不能删掉伪调用后冒充最终答案。"""

        async def stream_turn(self, call):
            attempts.append(call.messages[-1].text)
            polluted = (
                "先看文档，稍后再试。"
                f"<{_DSML} calls>"
                f'<{_DSML} invoke name="read_url">'
                f"</{_DSML} invoke></{_DSML} calls>"
            )
            yield ModelEvent(kind="text_delta", delta=polluted)
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")

    bound = BoundTools((
        ToolDefinition(name="read_url", source="builtin", input_schema={"type": "object"}),
    ), {})
    graph = build_agent_graph(Provider(), prepared(1), LangChainTools(bound))
    events = []
    with pytest.raises(AgentError, match="AGENT_MODEL_RESPONSE_INVALID"):
        async for event in graph.astream({}, stream_mode="custom"):
            events.append(event)
    assert len(attempts) == 2
    assert [event.code for event in events if event.kind == "warning"] == [
        "AGENT_MODEL_MARKUP_RETRIED",
    ]
    assert not any(event.kind == "done" for event in events)


@pytest.mark.parametrize("polluted,expected", [
    (f"先看文档，稍后再试。<{_DSML} calls>参数不可见</{_DSML} calls>", "先看文档，稍后再试。"),
    ("前缀<||DSML|| invoke name=\"x\">值</||DSML|| invoke>后缀", "前缀后缀"),
    (f"截断场景<{_DSML} calls>被截断的参数", "截断场景"),
    (f"<{_DSML} calls>只有标记</{_DSML} calls>", ""),
    ("无标记正文保持原样", "无标记正文保持原样"),
])
def test_strip_tool_call_markup_keeps_plain_text_only(polluted, expected):
    """剥离范围是首个开标记到最后一个闭标签；块内参数和截断尾部都不入回复。"""
    from agent_bot.services.reply_text import strip_tool_call_markup

    assert strip_tool_call_markup(polluted) == expected


async def test_other_invalid_responses_still_fail_without_retry():
    """非伪工具标记导致的响应异常不占用纠正额度，直接失败。"""
    from agent_bot.agent.errors import AgentError
    from agent_bot.agent.graph import build_agent_graph
    from agent_bot.agent.tools.langchain import LangChainTools

    attempts: list[str] = []

    class Provider:
        """正文干净但协议异常，不属于纠正重问范围。"""

        async def stream_turn(self, call):
            attempts.append(call.messages[-1].text)
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            yield  # pragma: no cover — 仅为构成异步生成器

    bound = BoundTools((
        ToolDefinition(name="read_url", source="builtin", input_schema={"type": "object"}),
    ), {})
    graph = build_agent_graph(Provider(), prepared(1), LangChainTools(bound))
    with pytest.raises(AgentError, match="AGENT_MODEL_RESPONSE_INVALID"):
        [event async for event in graph.astream({}, stream_mode="custom")]
    assert len(attempts) == 1


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
