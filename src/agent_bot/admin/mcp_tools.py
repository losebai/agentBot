"""管理端只读发现已登记 MCP 的全部工具，不执行工具或修改服务配置。"""

from time import monotonic

from agent_bot.agent.contracts import McpView, ToolDefinition
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials


async def list_server_tools(server: McpView) -> tuple[ToolDefinition, ...]:
    """使用已保存的端点和凭据发现工具，忽略执行白名单和启用状态以便恢复配置。"""
    deadline = monotonic() + server.timeout_seconds
    # 仅本次只读发现放开白名单；不得写回目录，也不能因非必需连接失败而返回假空列表。
    probe = server.model_copy(update={"allowed_tools": ("*",), "required": True})
    return await McpConnector(McpCredentials()).list_tools(
        probe, remaining=lambda: max(0, deadline - monotonic()),
    )
