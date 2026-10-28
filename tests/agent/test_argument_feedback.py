"""隔离模型与工具边界，验证参数错误可纠正、整批无副作用及失败记录可见。"""

import json
from contextlib import asynccontextmanager

import pytest

from agent.test_service import request, service_for
from agent_bot.agent.contracts import (
    ExecutionAuthority,
    ModelEvent,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.runtime import BoundTools


class CheckedTools:
    """真实参数预检与业务处理只调用本地替身，拒绝批次不得到达处理器。"""

    def __init__(self) -> None:
        """以可导致真实历史问题的数量上限作为工具约束。"""
        self.invoked = []
        self.bound = BoundTools((ToolDefinition(
            name="lookup", source="builtin", input_schema={
                "type": "object", "properties": {
                    "query": {"type": "string", "minLength": 1},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 5},
                    "api_key": {"type": "string"},
                }, "required": ["query"], "additionalProperties": False,
            },
        ),), {"lookup": self.call})

    @asynccontextmanager
    async def open(self, snapshot, current_request, authority, **media_context):
        """与服务保持同一工具生命周期，不绕过生产 BoundTools 校验。"""
        yield self.bound

    async def call(self, call: ToolCall) -> ToolResult:
        """保存实际执行的调用，验证前项不会在后项失败时提前执行。"""
        self.invoked.append(call)
        return ToolResult(name=call.name, call_id=call.call_id, content="查询事实")


class CorrectingProvider:
    """首次返回一好一坏参数，下一轮依据错误修正；不调用真实模型。"""

    def __init__(self, *, forbidden: bool = False) -> None:
        """可切换权限拒绝，验证该错误不能进入可纠正路径。"""
        self.calls = []
        self.forbidden = forbidden

    def validate_options(self, options) -> None:
        """模型替身接受测试配置。"""

    async def stream_turn(self, call):
        """使用原请求轮数决定收尾，不允许增加隐式重试轮数。"""
        self.calls.append(call)
        if call.final_only:
            yield ModelEvent(kind="text_delta", delta="根据已有事实回答")
            yield ModelEvent(kind="turn_end", turn=ModelTurn(
                model="offline", content="根据已有事实回答",
            ))
            return
        calls = (ToolCall(call_id="corrected", name="lookup", arguments={
            "query": "已修正", "limit": 2,
        }),) if len(self.calls) > 1 else (
            ToolCall(call_id="valid-but-skipped", name="lookup", arguments={"query": "有效"}),
            ToolCall(call_id="invalid", name="forbidden" if self.forbidden else "lookup",
                     arguments={"query": "待查", "limit": 99, "api_key": "private-input-key"}),
        )
        yield ModelEvent(kind="turn_end", turn=ModelTurn(model="offline", calls=calls))

    async def aclose(self) -> None:
        """替身没有外部资源。"""


@pytest.mark.parametrize("entry", ["qq", "admin"])
async def test_argument_preflight_records_failures_and_model_can_correct_within_budget(
    agent_database, agent_params, seeded_model, entry,
):
    """校验失败不终止整条会话，拒绝批次不执行，修正后仅执行新调用一次。"""
    tools, provider = CheckedTools(), CorrectingProvider()
    service = service_for(agent_database, agent_params, tools=tools, maximum_tool_calls=3)
    service.providers["openai-chat"] = provider
    events = [event async for event in service.stream(
        request().model_copy(update={"max_steps": 3}),
        ExecutionAuthority(entry=entry, allowed_tools=("lookup",)),
    )]
    assert events[-1].kind == "done"
    assert [call.call_id for call in tools.invoked] == ["corrected"]
    assert len(provider.calls) == 3 and provider.calls[-1].final_only
    feedback = [message.tool_result for message in provider.calls[1].messages
                if message.role == "tool"]
    assert [item.call_id for item in feedback] == ["valid-but-skipped", "invalid"]
    assert all(item.is_error for item in feedback)
    error = json.loads(feedback[-1].content)["error"]
    assert error["code"] == "AGENT_TOOL_ARGUMENTS_INVALID"
    assert error["issues"][0]["field"] == "limit"
    assert error["issues"][0]["rule"] == "maximum"
    assert "5" in error["issues"][0]["message"]
    assert "private-input-key" not in feedback[-1].content
    steps = (await service.records.list_steps(events[-1].execution_id)).items
    failures = [step for step in steps if step.status == "failed"]
    assert {step.call_id for step in failures} == {"invalid", "valid-but-skipped"}
    invalid = next(step for step in failures if step.call_id == "invalid")
    assert invalid.arguments == {"query": "待查", "limit": 99}
    assert invalid.result["phase"] == "validation" and invalid.result["executed"] is False
    assert invalid.result["validation_errors"] == error["issues"]
    assert not any(step.kind == "tool_start" and step.call_id == "invalid" for step in steps)
    assert "private-input-key" not in "".join(event.model_dump_json() for event in events)
    run = await service.records.get_run(events[-1].execution_id)
    assert run.status == "completed" and run.error_code is None


async def test_rejected_last_batch_uses_existing_final_round_without_extra_tools(
    agent_database, agent_params, seeded_model,
):
    """最后业务轮的错误只交给原有收尾轮，不能超出配置重新执行工具。"""
    tools, provider = CheckedTools(), CorrectingProvider()
    service = service_for(agent_database, agent_params, tools=tools)
    service.providers["openai-chat"] = provider
    result = await service.complete(
        request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",)),
    )
    assert result.content == "根据已有事实回答"
    assert tools.invoked == [] and len(provider.calls) == 2
    assert provider.calls[-1].final_only and provider.calls[-1].tools == ()
    assert any(message.role == "tool" and message.tool_result.is_error
               for message in provider.calls[-1].messages)


async def test_authorization_rejection_stays_fatal_and_does_not_execute_valid_peer(
    agent_database, agent_params, seeded_model,
):
    """未知或未授权工具仍立即失败，不能伪装成可修正参数问题。"""
    tools, provider = CheckedTools(), CorrectingProvider(forbidden=True)
    service = service_for(agent_database, agent_params, tools=tools)
    service.providers["openai-chat"] = provider
    with pytest.raises(AgentError, match="AGENT_TOOL_UNAUTHORIZED"):
        await service.complete(request(), ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
    assert tools.invoked == [] and len(provider.calls) == 1


@pytest.mark.parametrize("arguments,field,rule", [
    ({}, "query", "required"),
    ({"query": "正常", "limit": "private-invalid-value"}, "limit", "type"),
    ({"query": "正常", "extra": "private-invalid-value"}, "extra", "additionalProperties"),
])
def test_validation_feedback_identifies_fields_without_echoing_invalid_values(
    arguments, field, rule,
):
    """缺字段、类型和多余字段都提供定位，异常原文中的值不得泄露到反馈。"""
    from agent_bot.agent.tools.langchain import LangChainTools

    tools = LangChainTools(CheckedTools().bound)
    result = tools.validate_batch((ToolCall(name="lookup", call_id="bad", arguments=arguments),))
    issue = json.loads(result["bad"].content)["error"]["issues"][0]
    assert issue["field"] == field and issue["rule"] == rule
    assert "private-invalid-value" not in result["bad"].content


def test_local_business_combination_error_preserves_reason_without_raw_inputs() -> None:
    """JSON Schema 通过后的本地业务组合错误同样有明确说明且不回显正文。"""
    from agent_bot.agent.tools.langchain import LangChainTools
    from agent_bot.agent.tools.local_media import project_local_schema
    from agent_bot.services.tagged_memory import MemoryArguments

    definition = ToolDefinition(name="memory", source="chat", input_schema=project_local_schema(
        "memory", MemoryArguments.model_json_schema(),
    ))
    bound = BoundTools((definition,), {"memory": None})
    result = LangChainTools(bound).validate_batch((ToolCall(
        call_id="bad-save", name="memory", arguments={
            "action": "save", "scope": "user", "content": "private-invalid-value",
        },
    ),))
    issue = json.loads(result["bad-save"].content)["error"]["issues"][0]
    assert issue["rule"] == "value_error" and "save requires" in issue["message"]
    assert "private-invalid-value" not in result["bad-save"].content


def test_remote_search_uses_its_own_parameter_schema() -> None:
    """MCP 搜索仅校验服务端声明的 schema，不套用旧内置搜索的关键词约束。"""
    from agent_bot.agent.tools.langchain import LangChainTools

    checked = CheckedTools()
    definition = ToolDefinition(name="web_search", source="mcp", input_schema={
        "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"],
    })
    bound = BoundTools((*checked.bound.definitions, definition),
                       {**checked.bound.handlers, "web_search": None})
    results = LangChainTools(bound).validate_batch((
        ToolCall(call_id="good", name="lookup", arguments={"query": "正常"}),
        ToolCall(call_id="blank", name="web_search", arguments={"query": " \n\t "}),
    ))
    assert results == {}
    assert checked.invoked == []


async def test_repeated_invalid_arguments_do_not_add_implicit_model_rounds(
    agent_database, agent_params, seeded_model,
):
    """持续参数错误也只消耗原有业务轮数，最后进入无工具收尾。"""
    class InvalidProvider(CorrectingProvider):
        """每个业务轮都返回不同 ID 的非法参数，不修改最终收尾协议。"""

        async def stream_turn(self, call):
            """调用 ID 保持唯一，使失败原因仅来自参数而非重复调用保护。"""
            if call.final_only:
                async for event in super().stream_turn(call):
                    yield event
                return
            self.calls.append(call)
            yield ModelEvent(kind="turn_end", turn=ModelTurn(model="offline", calls=(
                ToolCall(call_id=f"bad-{len(self.calls)}", name="lookup",
                         arguments={"query": "待查", "limit": 99}),
            )))

    tools, provider = CheckedTools(), InvalidProvider()
    service = service_for(agent_database, agent_params, tools=tools, maximum_tool_calls=2)
    service.providers["openai-chat"] = provider
    result = await service.complete(request().model_copy(update={"max_steps": 2}),
                                    ExecutionAuthority(entry="qq", allowed_tools=("lookup",)))
    assert len(provider.calls) == 3 and provider.calls[-1].final_only
    assert tools.invoked == []
    steps = (await service.records.list_steps(result.execution_id)).items
    assert len([step for step in steps if step.status == "failed"]) == 2
