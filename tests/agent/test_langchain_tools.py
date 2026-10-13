"""框架工具接口必须保留调用身份、业务失败和整批预检。"""

import pytest

from agent_bot.agent.contracts import ToolCall, ToolDefinition, ToolResult
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.runtime import BoundTools


async def test_structured_tool_keeps_schema_and_call_id():
    """工具名、参数和调用 ID 原样到达业务层，错误不能被改成成功。"""
    from agent_bot.agent.tools.langchain import LangChainTools

    calls = []

    async def handle(call):
        """模拟具有失败结果的业务工具。"""
        calls.append(call)
        return ToolResult(call_id=call.call_id, name=call.name, content="failed", is_error=True)

    definition = ToolDefinition(
        name="lookup",
        source="builtin",
        input_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    tools = LangChainTools(BoundTools((definition,), {"lookup": handle}))
    call = ToolCall(call_id="identity-1", name="lookup", arguments={"query": "你好"})
    result = await tools.call(call)
    assert calls == [call]
    assert result.is_error and result.call_id == "identity-1" and result.content == "failed"
    assert "call_id" not in tools.definitions[0].args


async def test_later_invalid_call_prevents_entire_batch():
    """后项越权时不会提前执行前项。"""
    from agent_bot.agent.tools.langchain import LangChainTools

    calls = []

    async def handle(call):
        """任何提前执行都属于权限预检失败。"""
        calls.append(call)

    definition = ToolDefinition(name="lookup", source="builtin", input_schema={"type": "object"})
    tools = LangChainTools(BoundTools((definition,), {"lookup": handle}))
    with pytest.raises(AgentError):
        tools.validate_batch(
            (
                ToolCall(call_id="1", name="lookup", arguments={}),
                ToolCall(call_id="2", name="forbidden", arguments={}),
            )
        )
    assert calls == []


@pytest.mark.parametrize("field", ["config", "self", "callbacks", "run_manager", "arguments"])
async def test_remote_argument_does_not_collide_with_runtime_config(field):
    """远端工具可以合法声明框架保留参数，框架不能覆盖业务值。"""
    from agent_bot.agent.tools.langchain import LangChainTools

    async def handle(call):
        """回传模型原始业务参数，检查运行配置注入没有破坏它。"""
        return ToolResult(call_id=call.call_id, name=call.name, content=call.arguments[field])

    definition = ToolDefinition(name="configure", source="mcp", input_schema={
        "type": "object", "properties": {field: {"type": "string"}}, "required": [field],
    })
    tools = LangChainTools(BoundTools((definition,), {"configure": handle}))
    result = await tools.call(ToolCall(name="configure", call_id="c", arguments={field: "safe"}))
    assert result.content == "safe"
