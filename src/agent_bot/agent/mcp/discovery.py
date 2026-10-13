"""Finite MCP discovery and safe text result conversion."""

import asyncio

from mcp import types

from agent_bot.agent.contracts import ToolDefinition, ToolResult
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.schema import ToolSchema, bounded_json


async def discover(session, server, remaining):
    cursor, seen, tools = None, set(), []
    for _ in range(10):
        result = await asyncio.wait_for(
            session.list_tools(
                params=types.PaginatedRequestParams(cursor=cursor) if cursor else None
            ),
            min(server.timeout_seconds, remaining()),
        )
        for item in result.tools:
            if "*" not in server.allowed_tools and item.name not in server.allowed_tools:
                continue
            if len(item.description or "") > 2000:
                raise AgentError("AGENT_TOOL_SCHEMA_INVALID", status_code=422)
            tool = ToolDefinition(
                name=item.name,
                description=item.description or "",
                input_schema=item.input_schema,
                source="mcp",
            )
            ToolSchema.validate_definition(tool)
            if any(existing.name == tool.name for existing in tools):
                raise AgentError("AGENT_TOOL_NAME_CONFLICT", status_code=409)
            tools.append(tool)
            if len(tools) > 20:
                raise AgentError("AGENT_MCP_TOOL_LIMIT", status_code=422)
        cursor = result.next_cursor
        if not cursor:
            if not tools:
                raise AgentError("AGENT_TOOL_UNAVAILABLE", status_code=503)
            return tuple(tools)
        if cursor in seen:
            raise AgentError("AGENT_MCP_DISCOVERY_LIMIT", status_code=422)
        seen.add(cursor)
    raise AgentError("AGENT_MCP_DISCOVERY_LIMIT", status_code=422)


def convert_result(call, result):
    if not isinstance(result, types.CallToolResult):
        raise AgentError("AGENT_MCP_RESULT_UNSUPPORTED")
    if result.is_error:
        # Raw error text may contain server configuration or credentials.
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            content='{"code":"AGENT_REMOTE_TOOL_FAILED"}',
            is_error=True,
        )
    if any(not isinstance(item, types.TextContent) for item in result.content):
        raise AgentError("AGENT_MCP_RESULT_UNSUPPORTED")
    content = (
        bounded_json(result.structured_content, maximum=128000)
        if result.structured_content is not None
        else "\n".join(item.text for item in result.content)
    )
    if len(content) > 32000:
        raise AgentError("AGENT_TOOL_RESULT_TOO_LARGE")
    return ToolResult(call_id=call.call_id, name=call.name, content=content)
