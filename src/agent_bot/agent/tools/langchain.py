"""将请求授权后的工具绑定到 LangChain，保留业务身份和结果附件。"""

from contextvars import ContextVar

from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langsmith import tracing_context

from agent_bot.agent.contracts import ToolCall, ToolDefinition, ToolResult
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.runtime import BoundTools


class LangChainTools:
    """框架工具只接受已绑定能力，模型不能通过参数指定调用身份。"""

    def __init__(self, bound: BoundTools) -> None:
        """定义和 handler 共用同一个请求授权快照。"""
        self.bound = bound
        self._active_call: ContextVar[ToolCall] = ContextVar("agent_tool_call")
        self.definitions = tuple(self._build(definition) for definition in bound.definitions)
        self.by_name = {tool.name: tool for tool in self.definitions}

    def _build(self, definition: ToolDefinition) -> StructuredTool:
        """执行层封装参数以避开框架保留名，模型仍绑定原始业务 Schema。"""

        async def invoke(arguments: dict) -> ToolMessage:
            """实际调用时再次检查 QQ 令牌及参数，结果保留原始错误状态。"""
            # 调用身份不来自模型参数；参数封装保护 self/config 等合法业务字段。
            call = self._active_call.get()
            call_id = call.call_id
            result = await self.bound.call(call)
            if result.call_id != call_id or result.name != definition.name:
                raise AgentError("AGENT_TOOL_RESULT_INVALID")
            return ToolMessage(
                content=result.content,
                name=result.name,
                tool_call_id=call_id,
                artifact=result,
                status="error" if result.is_error else "success",
            )

        return StructuredTool(
            name=definition.name,
            description=definition.description,
            args_schema={
                "type": "object",
                "properties": {"arguments": definition.input_schema},
                "required": ["arguments"],
                "additionalProperties": False,
            },
            coroutine=invoke,
        )

    def validate_batch(self, calls: tuple[ToolCall, ...]) -> None:
        """整批调用全部验证通过后，调用方才可以开始任何副作用。"""
        for call in calls:
            self.bound.validate(call)

    async def call(self, call: ToolCall) -> ToolResult:
        """通过真实 LangChain 异步工具接口执行，附件仅走 artifact 通道。"""
        if call.name not in self.by_name:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        token = self._active_call.set(call)
        try:
            with tracing_context(enabled=False):
                message = await self.by_name[call.name].ainvoke(
                    {
                        "id": call.call_id,
                        "name": call.name,
                        "args": {"arguments": call.arguments},
                        "type": "tool_call",
                    },
                )
        finally:
            self._active_call.reset(token)
        return message.artifact
