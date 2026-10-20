"""通过现有令牌作用域的 Chat MCP 执行 QQ 工具，保留原投递和队列语义。"""

import json

from mcp import types

from agent_bot.agent.contracts import ToolDefinition, ToolResult
from agent_bot.agent.errors import AgentError
from agent_bot.agent.mcp.discovery import convert_result
from agent_bot.agent.tools.local_media import project_local_schema, validate_local_call
from agent_bot.agent.tools.policy import CHAT_TOOLS
from agent_bot.agent.tools.schema import ToolSchema
from agent_bot.services.tagged_memory import MEMORY_SOURCE_ERROR_MESSAGE
from agent_bot.services.tool_sessions import ToolSessionExpiredError


class ChatToolBridge:
    """以有效 QQ 会话令牌桥接工具调用，保留原有权限和副作用边界。"""
    def __init__(self, server):
        """保存可热更新的聊天 MCP 服务提供器。"""
        self._server = server

    def _access(self, authority):
        """只接受 QQ 入口签发的请求令牌。"""
        if authority.entry != "qq" or authority.chat_token is None:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        server = self._server() if callable(self._server) else self._server
        if server is None:
            raise AgentError("AGENT_CHAT_TOOLS_UNAVAILABLE", status_code=503)
        return server, authority.chat_token.get_secret_value()

    async def context(self, authority):
        """向本地 Job 提供认证上下文，不允许模型指定身份。"""
        server, token = self._access(authority)
        try:
            return await server.resolve_context(token)
        except ToolSessionExpiredError as error:
            raise AgentError("AGENT_TOOL_SESSION_EXPIRED", status_code=403) from error

    async def list_tools(self, authority):
        """令牌每次重新验证，本地 schema 投影不改变服务端原始约束。"""
        server, token = self._access(authority)
        try:
            result = await server.list_tools(token)
        except ToolSessionExpiredError as error:
            raise AgentError("AGENT_TOOL_SESSION_EXPIRED", status_code=403) from error
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
            raise AgentError("AGENT_TOOL_SESSION_EXPIRED", status_code=403) from error
        # 只公开本地记忆来源的固定提示；其他错误继续遮蔽，不透传服务器异常正文。
        if (call.name == "memory" and isinstance(result, types.CallToolResult)
                and result.is_error and len(result.content) == 1
                and isinstance(result.content[0], types.TextContent)):
            try:
                body = json.loads(result.content[0].text)
            except (ValueError, TypeError):
                body = None
            if isinstance(body, dict) and body.get("code") == "MEMORY_SOURCE_INVALID":
                return ToolResult(
                    call_id=call.call_id, name=call.name, is_error=True,
                    content=json.dumps({"code": "MEMORY_SOURCE_INVALID",
                                        "message": MEMORY_SOURCE_ERROR_MESSAGE},
                                       ensure_ascii=False),
                )
        return convert_result(call, result)
