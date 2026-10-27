"""协议失败日志提供可定位元数据，不泄露 MCP 令牌或上游错误正文。"""

import pytest

from agent_bot.agent.errors import AgentError
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials

from .mcp_server import peer
from .test_mcp import server


async def test_http_protocol_failure_logs_status_and_initialization_stage(caplog):
    """真实 HTTP 失败须能区分状态码及初始化阶段，令牌不进入日志。"""
    async with peer(status=500) as (url, _state):
        with pytest.raises(AgentError, match="AGENT_MCP_PROTOCOL_ERROR"):
            async with McpConnector(McpCredentials()).open(
                (server(url, credential="private-mcp-token"),), remaining=lambda: 5,
            ):
                pytest.fail("协议失败不能进入已连接状态")
    diagnostics = "\n".join(record.getMessage() for record in caplog.records
                            if record.name.startswith("agent_bot.agent.mcp"))
    assert "status=500" in diagnostics
    assert "service=docs" in diagnostics
    assert "stage=initialize" in diagnostics
    assert "private-mcp-token" not in caplog.text
