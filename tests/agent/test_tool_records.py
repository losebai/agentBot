"""真实执行和仓储验证工具入参、返回值、取消状态及记录脱敏。"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from agent.test_engine import Provider, Tools
from agent.test_service import request, service_for
from agent_bot.agent.contracts import (
    AgentModelCall,
    ExecutionAuthority,
    ModelEvent,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent_bot.ai.call_records import CallRecordStore
from agent_bot.settings import DatabaseParams


@pytest.mark.parametrize("name", ["image_generate", "local_workflow_generate_image"])
async def test_queued_image_archives_success_without_another_model_call(
    agent_database: Any, agent_params: DatabaseParams, seeded_model: Any, name: str,
) -> None:
    """图片入队经真实服务和仓储记为成功，工具步骤仍保留提交回执。"""
    class QueuedProvider(Provider):
        """仅首轮提交工具，任何后续模型请求都代表不应发生的等待。"""

        async def stream_turn(self, call: AgentModelCall) -> AsyncIterator[ModelEvent]:
            """模型没有文本草稿时，也应能成功完成父请求。"""
            self.calls.append(call)
            assert len(self.calls) == 1, "queued image must not start another model turn"
            arguments = {"start_reply": "开始画猫。"}
            arguments.update({"prompt": "猫"} if name == "image_generate" else {
                "workflow_key": "cat", "positive_prompt": "cat",
            })
            yield ModelEvent(kind="turn_end", turn=ModelTurn(model="actual", calls=(
                ToolCall(call_id="image", name=name, arguments=arguments),
            )))

    class QueuedTools(Tools):
        """只替换后台提交端口，资源租约和记录使用真实服务逻辑。"""

        definitions = (ToolDefinition(name=name, source="chat", input_schema={"type": "object"}),)

        async def call(self, call: ToolCall) -> ToolResult:
            """入队回执与实际本地绘图工具协议相同。"""
            return ToolResult(call_id=call.call_id, name=call.name,
                              content='{"status":"queued","task_id":"image-task"}')

    tools = QueuedTools()
    service = service_for(agent_database, agent_params, tools=tools)
    service.providers["openai-chat"] = QueuedProvider()
    result = await service.complete(
        request(), ExecutionAuthority(entry="qq", allowed_tools=(name,)),
    )
    saved = await service.records.get_run(result.execution_id)
    steps = (await service.records.list_steps(result.execution_id)).items
    assert saved.status == "completed" and saved.error_code is None
    assert saved.content == result.content and result.content.strip()
    assert len([step for step in steps if step.kind == "model_start"]) == 1
    end = next(step for step in steps if step.kind == "tool_result")
    assert end.status == "completed" and json.loads(end.result["content"])["status"] == "queued"
    assert tools.active == service.active_requests == 0


class ArgumentProvider(Provider):
    """受控模型生成具有业务参数的工具调用，不替换执行与保存逻辑。"""

    async def stream_turn(self, call):
        """保持原模型文本流，给首次工具调用附加实际参数。"""
        async for event in super().stream_turn(call):
            if event.kind == "turn_end" and event.turn.calls:
                event = event.model_copy(
                    update={
                        "turn": event.turn.model_copy(
                            update={
                                "calls": (
                                    ToolCall(
                                        call_id="lookup-1",
                                        name="lookup",
                                        arguments={
                                            "query": "今日天气",
                                            "limit": 2,
                                            "api_key": "private-input-key",
                                        },
                                    ),
                                ),
                            }
                        )
                    }
                )
            yield event


class ReturningTools(Tools):
    """仅替换外部工具端口，返回结构化业务内容和可控错误状态。"""

    def __init__(self, *, failed=False):
        """控制失败返回，不连接真实 MCP 或 QQ。"""
        super().__init__()
        self.failed = failed
        self.arguments = None

    async def call(self, call):
        """保存端口实际收到的入参，检测脱敏是否意外改动执行参数。"""
        self.arguments = call.arguments
        self.invoked.append(call.call_id)
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            is_error=self.failed,
            content=json.dumps({"city": "上海", "temperature": 23, "token": "private-output-key"}),
        )


@pytest.mark.parametrize("entry", ["qq", "admin"])
async def test_tool_payloads_saved_and_visible_in_call_detail(
    agent_database,
    agent_params,
    seeded_model,
    entry,
):
    """丢失事件入参、返回值或统一详情读取会使此用例失败。"""
    tools = ReturningTools()
    service = service_for(agent_database, agent_params, tools=tools)
    service.providers["openai-chat"] = ArgumentProvider()
    result = await service.complete(
        request(), ExecutionAuthority(entry=entry, allowed_tools=("lookup",))
    )
    steps = (await service.records.list_steps(result.execution_id)).items
    start = next(step for step in steps if step.kind == "tool_start")
    end = next(step for step in steps if step.kind == "tool_result")
    assert start.arguments == end.arguments == {"query": "今日天气", "limit": 2}
    assert start.result is None
    assert json.loads(end.result["content"]) == {"city": "上海", "temperature": 23}
    assert end.result["is_error"] is False
    assert tools.arguments["api_key"] == "private-input-key"
    stored = agent_database.connection.execute(
        "SELECT arguments,result FROM agent_bot_agent_step WHERE kind='tool_result'"
    ).fetchone()
    assert "private-output-key" not in stored["result"]
    assert "private-input-key" not in stored["arguments"]
    unified = CallRecordStore(agent_params, connect=agent_database.connect)
    detail = await unified.get_call((await unified.list_calls(type="agent")).items[0].id)
    assert [step.call_id for step in detail.tool_steps] == ["lookup-1", "lookup-1"]
    assert detail.tool_steps[-1].result == end.result


async def test_failed_tool_result_retains_actual_content(
    agent_database, agent_params, seeded_model
):
    """工具主动返回失败时，错误正文不能被固定摘要替代。"""
    service = service_for(agent_database, agent_params, tools=ReturningTools(failed=True))
    service.providers["openai-chat"] = ArgumentProvider()
    result = await service.complete(
        request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",))
    )
    end = next(
        step
        for step in (await service.records.list_steps(result.execution_id)).items
        if step.kind == "tool_result"
    )
    assert end.status == "failed" and end.result["is_error"] is True
    assert json.loads(end.result["content"])["temperature"] == 23


async def test_cancelled_tool_keeps_arguments_and_reports_missing_return(
    agent_database,
    agent_params,
    seeded_model,
):
    """取消后仍能看到输入，但不得伪造工具已经返回或自动重执行。"""
    happened = asyncio.Event()

    class WaitingTools(Tools):
        """保留可观测副作用后等待取消。"""

        async def call(self, call):
            """没有产生返回值，因此只保留未知结果和取消码。"""
            self.invoked.append(call.call_id)
            happened.set()
            await asyncio.Event().wait()

    tools = WaitingTools()
    service = service_for(agent_database, agent_params, tools=tools)
    service.providers["openai-chat"] = ArgumentProvider()
    task = asyncio.create_task(
        service.complete(
            request(),
            ExecutionAuthority(
                entry="qq",
                allowed_tools=("lookup",),
            ),
        )
    )
    await asyncio.wait_for(happened.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = (await service.records.list_runs()).items[0]
    end = (await service.records.list_steps(run.execution_id)).items[-1]
    assert end.status == "unknown" and end.arguments == {"query": "今日天气", "limit": 2}
    assert end.result["error_code"] == "AGENT_CANCELLED"
    assert end.result["outcome"] == "unknown" and "content" not in end.result
    assert tools.invoked == ["lookup-1"]


async def test_tool_payloads_are_not_serialized_in_public_stream(
    agent_database, agent_params, seeded_model,
):
    """内部记录字段不能改变 SSE 正文或泄露未脱敏参数和工具返回。"""
    service = service_for(agent_database, agent_params, tools=ReturningTools())
    service.providers["openai-chat"] = ArgumentProvider()
    events = [event async for event in service.stream(
        request(), ExecutionAuthority(entry="admin", allowed_tools=("lookup",)),
    )]
    tool_start = next(event for event in events if event.kind == "tool_start")
    assert tool_start.arguments["api_key"] == "private-input-key"
    wire = "".join(event.model_dump_json() for event in events)
    assert "private-input-key" not in wire and "private-output-key" not in wire
    assert events[-1].kind == "done" and events[-1].metadata.record_saved
