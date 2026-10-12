"""通过现有令牌作用域的 Chat MCP 执行 QQ 工具，保留原投递和队列语义。"""

from agent_bot.agent.contracts import ToolDefinition
from agent_bot.agent.errors import AgentError
from agent_bot.agent.mcp.discovery import convert_result
from agent_bot.agent.tools.local_media import project_local_schema, validate_local_call
from agent_bot.agent.tools.policy import CHAT_TOOLS
from agent_bot.agent.tools.schema import ToolSchema
from agent_bot.services.tool_sessions import ToolSessionExpiredError


class ChatToolBridge:
    """以有效 QQ 会话令牌桥接工具调用，保留原有权限和副作用边界。"""
    def __init__(self, server):
        self._server = server

    def _access(self, authority):
        if authority.entry != "qq" or authority.chat_token is None:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        server = self._server() if callable(self._server) else self._server
        if server is None:
            raise AgentError("AGENT_CHAT_TOOLS_UNAVAILABLE", status_code=503)
        return server, authority.chat_token.get_secret_value()

    async def list_tools(self, authority):
        """令牌每次重新验证，本地 schema 投影不改变服务端原始约束。"""
        server, token = self._access(authority)
        try:
            result = await server.list_tools(token)
        except ToolSessionExpiredError as error:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403) from error
        return tuple(
            ToolDefinition(
                name=item.name,
                description=item.description or "",
                input_schema=project_local_schema(item.name, item.input_schema),
                source="chat",
            )
            for item in result.tools
            if item.name in CHAT_TOOLS and item.name in authority.allowed_tools
        )

    async def call(self, call, authority):
        """入口、当前令牌、schema 和原媒体参数都通过后才触发副作用。"""
        server, token = self._access(authority)
        tools = await self.list_tools(authority)
        tool = next((t for t in tools if t.name == call.name), None)
        if tool is None:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        ToolSchema.validate_arguments(tool, call.arguments)
        validate_local_call(call)
        try:
            result = await server.call_tool(token, call.name, call.arguments)
        except ToolSessionExpiredError as error:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403) from error
        return convert_result(call, result)
