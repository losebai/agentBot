"""有界发现 MCP 工具，转换文本和经过真实解码校验的图片结果。"""

import asyncio
import base64
import binascii

from mcp import types

from agent_bot.agent.contracts import MAX_MCP_TOOLS, ToolDefinition, ToolResult
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.schema import ToolSchema, bounded_json
from agent_bot.media.config import MAX_IMAGE_BYTES, MAX_IMAGES, MAX_TOTAL_IMAGE_BYTES
from agent_bot.media.errors import MediaError
from agent_bot.media.inputs import ImagePreparer

# 管理目录允许先展示较大的工具集合，执行期仍按 32 项预算收紧。
MAX_MCP_CATALOG_TOOLS = 256


async def discover(session, server, remaining, *, catalog=False):
    """执行按白名单校验结构；管理只读目录展示全部定义，超限均明确拒绝。"""
    maximum = MAX_MCP_CATALOG_TOOLS if catalog else MAX_MCP_TOOLS
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
            if len(item.description or "") > (16000 if catalog else 2000):
                raise AgentError("AGENT_TOOL_SCHEMA_INVALID", status_code=422)
            tool = ToolDefinition(
                name=item.name,
                description=item.description or "",
                input_schema=item.input_schema,
                source="mcp",
            )
            if catalog:
                # 管理员需能看到尚不兼容的工具才能将其关闭；展示不能触发 schema 引用解析。
                bounded_json(tool.input_schema)
            else:
                ToolSchema.validate_definition(tool)
            if any(existing.name == tool.name for existing in tools):
                raise AgentError("AGENT_TOOL_NAME_CONFLICT", status_code=409)
            tools.append(tool)
            if len(tools) > maximum:
                raise AgentError(
                    "AGENT_MCP_DISCOVERY_LIMIT" if catalog else "AGENT_MCP_TOOL_LIMIT",
                    status_code=422,
                )
        cursor = result.next_cursor
        if not cursor:
            if not tools and not catalog:
                raise AgentError("AGENT_TOOL_UNAVAILABLE", status_code=503)
            return tuple(tools)
        if cursor in seen:
            raise AgentError("AGENT_MCP_DISCOVERY_LIMIT", status_code=422)
        seen.add(cursor)
    raise AgentError("AGENT_MCP_DISCOVERY_LIMIT", status_code=422)


def convert_result(call, result):
    """图片仅留在当前模型上下文，沿用媒体校验与预算，不写入工具记录正文。"""
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
    if any(
        not isinstance(item, (types.TextContent, types.ImageContent)) for item in result.content
    ):
        raise AgentError("AGENT_MCP_RESULT_UNSUPPORTED")
    image_blocks = [item for item in result.content if isinstance(item, types.ImageContent)]
    if len(image_blocks) > MAX_IMAGES:
        raise AgentError("MEDIA_TOO_MANY_IMAGES", status_code=422)
    images, total = [], 0
    for item in image_blocks:
        if len(item.data) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
            raise AgentError("MEDIA_TOO_LARGE", status_code=413)
        try:
            data = base64.b64decode(item.data, validate=True)
        except (binascii.Error, ValueError) as error:
            raise AgentError("MEDIA_INVALID_IMAGE", status_code=422) from error
        total += len(data)
        if total > MAX_TOTAL_IMAGE_BYTES:
            raise AgentError("MEDIA_TOO_LARGE", status_code=413)
        try:
            images.append(ImagePreparer().prepare(data, item.mime_type).model_image)
        except MediaError as error:
            raise AgentError(error.code, status_code=error.status_code) from error
    content = (
        bounded_json(result.structured_content, maximum=128000)
        if result.structured_content is not None
        else "\n".join(item.text for item in result.content if isinstance(item, types.TextContent))
    )
    if len(content) > 32000:
        raise AgentError("AGENT_TOOL_RESULT_TOO_LARGE")
    return ToolResult(call_id=call.call_id, name=call.name, content=content, images=tuple(images))
