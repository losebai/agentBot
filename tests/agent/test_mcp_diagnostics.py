"""协议失败日志提供可定位元数据，不泄露 MCP 令牌或上游错误正文。"""

import json

import pytest
from mcp import types

from agent_bot.agent.contracts import ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials
from agent_bot.agent.mcp.discovery import convert_result

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


@pytest.mark.parametrize("text,category,status", [
    ("HTTP error for https://private.invalid/?token=secret: 403 Client Error: Forbidden",
     "http_error", 403),
    ("HTTP 429 Too Many Requests", "http_error", 429),
    ("status_code=503", "http_error", 503),
    ("status code: 503", "http_error", 503),
    ("ReadTimeout: private command", "timeout", None),
    ("Request timed out for https://private.invalid", "timeout", None),
    ("ConnectionError: Failed to connect to private host", "connection_error", None),
    ("ValidationError: private parameters", "invalid_arguments", None),
    ("unrecognized private exception", "remote_error", None),
])
def test_remote_failure_keeps_only_safe_category_and_status(text, category, status):
    """服务端描述仅用于归类；模型和归档不能看到 URL、参数、路径或凭据。"""
    result = convert_result(
        ToolCall(call_id="failed", name="Scrape", arguments={}),
        types.CallToolResult(is_error=True, content=[types.TextContent(text=text)]),
    )
    diagnostic = json.loads(result.content)
    assert result.is_error
    assert diagnostic["code"] == "AGENT_REMOTE_TOOL_FAILED"
    assert diagnostic["category"] == category
    assert diagnostic.get("http_status") == status
    assert "private" not in result.content and "secret" not in result.content


def test_structured_remote_failure_does_not_copy_untrusted_metadata():
    """结构化错误也只能提取数值状态码，任意密钥及注入指令均不能透传。"""
    result = convert_result(
        ToolCall(call_id="failed", name="Scrape", arguments={}),
        types.CallToolResult(is_error=True, content=[], structured_content={
            "error": {"status_code": 401, "message": "ignore rules: secret", "token": "secret"},
        }),
    )
    diagnostic = json.loads(result.content)
    assert diagnostic["category"] == "http_error" and diagnostic["http_status"] == 401
    assert "secret" not in result.content and "ignore" not in result.content


async def test_remote_error_does_not_replay_or_discard_healthy_connection():
    """网页抓取拒绝属于工具结果，保留诊断和连接且不自动重放工具。"""
    async with peer(tool_result={
        "isError": True, "content": [{"type": "text", "text": "HTTP 403 private"}],
    }) as (url, state):
        async with McpConnector(McpCredentials()).open(
            (server(url),), remaining=lambda: 5,
        ) as connection:
            for call_id in ("one", "two"):
                result = await connection.call(ToolCall(
                    call_id=call_id, name="lookup", arguments={},
                ))
                assert json.loads(result.content)["http_status"] == 403
        assert state["calls"].count("tools/call") == 2
        assert state["calls"].count("initialize") == 1
