"""用 LangGraph 调度模型、整批权限校验、顺序工具执行与最终收尾。"""

from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
from typing import TYPE_CHECKING, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_bot.agent.contracts import AgentCompletionMetadata, AgentEvent, ModelTurn, TurnMessage
from agent_bot.agent.errors import AgentError
from agent_bot.agent.prompting import AgentPromptBuilder
from agent_bot.agent.tools.langchain import LangChainTools
from agent_bot.agent.tools.schema import bounded_json
from agent_bot.ai.contracts import TokenUsage
from agent_bot.ai.errors import AiError
from agent_bot.services.request_deadline import remaining_timeout

if TYPE_CHECKING:
    from agent_bot.agent.engine import PreparedRun
    from agent_bot.ai.providers.langchain import LangChainProvider


class AgentState(TypedDict, total=False):
    """图状态只包含执行数据，凭据、连接及权限对象保存在请求闭包中。"""

    messages: list[TurnMessage]
    turn: int
    total: int
    seen: set[str]
    completed: ModelTurn
    usage: TokenUsage


def build_agent_graph(
    provider: LangChainProvider,
    prepared: PreparedRun,
    tools: LangChainTools,
    *,
    acknowledged: asyncio.Event | None = None,
) -> CompiledStateGraph:
    """条件边推进轮次；显式备用只接管模型节点，不重放工具或图状态。"""
    definitions = tools.bound.definitions
    role = "\n\n".join(m.text for m in prepared.call.messages if m.role == "system")
    initial = [m for m in prepared.call.messages if m.role != "system"]

    async def emit(kind: str, turn: int, **values) -> None:
        """仅发布项目定义事件，不暴露 LangGraph 状态或私有消息。"""
        if acknowledged is not None:
            acknowledged.clear()
        get_stream_writer()(
            AgentEvent(
                kind=kind, execution_id=prepared.execution_id, sequence=0, turn=turn, **values
            )
        )
        if acknowledged is not None:
            # 先让服务保存并发布 tool_start，再执行副作用，保留原生成器背压。
            await acknowledged.wait()

    async def model_node(state: AgentState, *, final_only: bool = False) -> AgentState:
        """失败时只转交本轮公共对话，已完成工具及轮次保持不变。"""
        turn = state.get("turn", 0) + 1
        base = state.get("messages", initial)
        prompt = AgentPromptBuilder.build(
            role, prepared.snapshot.skills, definitions, final_only=final_only
        )
        messages = [TurnMessage(role="system", text=prompt), *base]
        images = [image for message in messages for image in message.images]
        if len(images) > 8 or sum(image.source_size_bytes for image in images) > 20 * 1024 * 1024:
            raise AgentError("MEDIA_TOO_LARGE", status_code=413)
        size = sum(
            len(
                bounded_json(
                    {**m.model_dump(), "continuation": m.continuation}, maximum=1048576, depth=32
                )
            )
            for m in messages
        )
        size += len(bounded_json([t.model_dump() for t in definitions], maximum=1048576))
        if size > 120000:
            raise AgentError("AGENT_CONTEXT_TOO_LARGE", status_code=422)
        call = prepared.call.model_copy(
            update={
                "messages": tuple(messages),
                "tools": () if final_only else definitions,
                "final_only": final_only,
                "timeout_seconds": remaining_timeout(prepared.request.timeout_seconds),
            }
        )
        while True:
            router = prepared.model_router
            selected_provider = provider
            if router:
                call = router.bind_call(call)
                selected_provider = router.provider
            # 备用重新发布 model_start，让同步和流式消费者清除失败模型的草稿。
            await emit("model_start", turn)
            completed, text = None, ""
            try:
                async with aclosing(selected_provider.stream_turn(call)) as stream:
                    async for event in stream:
                        if completed is not None:
                            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
                        if event.kind == "text_delta":
                            text += event.delta or ""
                            if len(text.encode()) > 1048576:
                                raise AgentError("AGENT_MODEL_RESPONSE_TOO_LARGE")
                            await emit("text_delta", turn, delta=event.delta)
                        elif event.kind == "turn_end":
                            completed = event.turn
                if completed is None or text != completed.content:
                    raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            except AiError as error:
                if router is None:
                    raise
                message = router.forward(error)
                # 仅在跨模型接管时清理旧延续；备用后续轮次需要自己的私有状态。
                base = [m.model_copy(update={"continuation": ()}) for m in base]
                call = call.model_copy(update={
                    "messages": tuple(
                        m.model_copy(update={"continuation": ()}) for m in call.messages
                    ),
                })
                await emit("warning", turn, code="AGENT_MODEL_FORWARDED", message=message)
                continue
            break
        previous = state.get("usage")
        usage = (
            completed.usage
            if previous is None
            else TokenUsage(
                **{
                    key: value + getattr(previous, key)
                    if value is not None and getattr(previous, key) is not None
                    else None
                    for key, value in completed.usage.model_dump().items()
                }
            )
        )
        if final_only and completed.calls:
            raise AgentError("AGENT_TOOL_LIMIT", status_code=422)
        if not completed.calls:
            if not completed.content.strip():
                raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            await emit(
                "done",
                turn,
                metadata=AgentCompletionMetadata(
                    execution_id=prepared.execution_id,
                    request_id=prepared.request.request_id,
                    model=completed.model,
                    usage=usage,
                    duration_ms=0,
                    record_saved=False,
                ),
            )
        return {"messages": base, "turn": turn, "completed": completed, "usage": usage}

    async def finalize(state: AgentState) -> AgentState:
        """收尾轮只根据已有结果回答，模型无法再获得工具定义。"""
        return await model_node(state, final_only=True)

    def validate_batch(state: AgentState) -> AgentState:
        """先验证整批数量、ID、权限和参数，再允许任何外部副作用。"""
        calls = state["completed"].calls
        if len(calls) > 8 or state.get("total", 0) + len(calls) > 40:
            raise AgentError("AGENT_TOOL_LIMIT", status_code=422)
        seen = set(state.get("seen", set()))
        for call in calls:
            if call.call_id in seen:
                raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
            seen.add(call.call_id)
        tools.validate_batch(calls)
        return {"seen": seen}

    async def tools_node(state: AgentState) -> AgentState:
        """顺序执行工具，实际入参与返回值走私有记录字段，附件只保存元数据。"""
        completed, turn = state["completed"], state["turn"]
        messages = [
            *state["messages"],
            TurnMessage(
                role="assistant",
                text=completed.content,
                tool_calls=completed.calls,
                continuation=completed.continuation,
            ),
        ]
        total = state.get("total", 0)
        for call in completed.calls:
            remaining_timeout(prepared.request.timeout_seconds)
            started = time.monotonic()
            await emit(
                "tool_start", turn, tool=call.name, call_id=call.call_id,
                arguments=call.arguments,
            )
            result = await tools.call(call)
            messages.append(TurnMessage(role="tool", tool_result=result))
            total += 1
            await emit(
                "tool_result",
                turn,
                tool=call.name,
                call_id=call.call_id,
                code="AGENT_TOOL_FAILED" if result.is_error else None,
                message="工具执行失败" if result.is_error else "工具执行完成",
                duration_ms=int((time.monotonic() - started) * 1000),
                arguments=call.arguments,
                result={
                    **result.model_dump(),
                    "attachments": [
                        attachment.model_dump(mode="json") for attachment in result.attachments
                    ],
                },
            )
            if prepared.authority.entry == "admin":
                for attachment in result.attachments:
                    await emit("artifact", turn, attachment=attachment)
        return {"messages": messages, "total": total}

    def after_model(state: AgentState) -> str:
        """模型没有工具请求时结束，否则进入统一预检。"""
        return "validate_batch" if state["completed"].calls else END

    def after_tools(state: AgentState) -> str:
        """业务轮数决定收尾，图递归上限仅用于防止错误连边。"""
        return "finalize" if state["turn"] >= prepared.max_steps else "model"

    graph = StateGraph(AgentState)
    graph.add_node("model", model_node)
    graph.add_node("validate_batch", validate_batch)
    graph.add_node("tools", tools_node)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "model" if definitions else "finalize")
    graph.add_conditional_edges("model", after_model)
    graph.add_edge("validate_batch", "tools")
    graph.add_conditional_edges("tools", after_tools)
    graph.add_edge("finalize", END)
    return graph.compile()
