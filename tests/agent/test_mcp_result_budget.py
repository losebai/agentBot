"""通过本地 MCP 和真实 Agent 归档验证长结果预算及明确的响应拒绝状态。"""

import json

import pytest

from agent_bot.agent.contracts import ExecutionAuthority, McpCreate
from agent_bot.agent.errors import AgentError
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials
from agent_bot.agent.tools.runtime import ToolRuntime

from .mcp_server import peer
from .test_service import request, service_for


@pytest.mark.parametrize("structured,chars,maximum", [
    (False, 40_000, 120_000),
    (True, 50_000, 120_000),
    (True, 400_000, 500_000),
    (False, 80_000, 60_000),
])
async def test_remote_result_uses_total_context_budget_without_hidden_limits(
    agent_database, agent_params, seeded_model, structured, chars, maximum,
):
    """结果不受旧字符或 UTF-8 字节上限影响；超总预算不重放已完成工具。"""
    text = "新" * chars
    payload = {"content": [{"type": "text", "text": text}], "isError": False}
    if structured:
        payload["structuredContent"] = {"article": text}
    async with peer(tool_result=payload) as (url, state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)
        service = service_for(
            agent_database, agent_params, tools=ToolRuntime(connector),
            maximum_context_chars=maximum,
        )
        try:
            await service.catalog.create_mcp(McpCreate(
                code="docs", name="离线抓取", endpoint=url + "/mcp", enabled=True,
                allowed_tools=("lookup",),
            ))
            incoming = request().model_copy(update={"mcp_services": ("docs",)})
            authority = ExecutionAuthority(entry="qq", allowed_mcp_services=("docs",))
            if chars > maximum:
                with pytest.raises(AgentError, match="AGENT_CONTEXT_TOO_LARGE"):
                    await service.complete(incoming, authority)
                assert len(service.providers["openai-chat"].calls) == 1
            else:
                result = await service.complete(incoming, authority)
                assert result.content == "最终回答" and result.record_saved
                model_calls = service.providers["openai-chat"].calls
                assert len(model_calls) == 2
                body = next(
                    m.tool_result.content for m in model_calls[-1].messages if m.tool_result
                )
                assert (json.loads(body)["article"] if structured else body) == text

            run = (await service.records.list_runs()).items[0]
            assert run.error_code == ("AGENT_CONTEXT_TOO_LARGE" if chars > maximum else None)
            steps = (await service.records.list_steps(run.execution_id)).items
            tool_steps = [step for step in steps if step.kind == "tool_result"]
            assert len(tool_steps) == 1 and tool_steps[0].status == "completed"
            assert tool_steps[0].summary == ""
            assert len(state["tool_requests"]) == 1
        finally:
            await service.aclose()
            await connector.aclose()


async def test_received_invalid_result_is_failed_instead_of_unknown(
    agent_database, agent_params, seeded_model,
):
    """远端已经返回的无效图片保留校验错误，不能记录成执行结果未知或重试。"""
    payload = {"content": [{"type": "image", "mimeType": "image/png", "data": "bad!"}]}
    async with peer(tool_result=payload) as (url, state):
        connector = McpConnector(McpCredentials(), reuse_connections=True)
        service = service_for(agent_database, agent_params, tools=ToolRuntime(connector))
        try:
            await service.catalog.create_mcp(McpCreate(
                code="docs", name="离线截图", endpoint=url + "/mcp", enabled=True,
                allowed_tools=("lookup",),
            ))
            with pytest.raises(AgentError, match="MEDIA_INVALID_IMAGE"):
                await service.complete(
                    request().model_copy(update={"mcp_services": ("docs",)}),
                    ExecutionAuthority(entry="qq", allowed_mcp_services=("docs",)),
                )
            run = (await service.records.list_runs()).items[0]
            assert run.status == "failed" and run.error_code == "MEDIA_INVALID_IMAGE"
            steps = (await service.records.list_steps(run.execution_id)).items
            result = steps[-1]
            assert result.kind == "tool_result" and result.status == "failed"
            assert result.summary == "MEDIA_INVALID_IMAGE"
            assert result.result == {
                "error_code": "MEDIA_INVALID_IMAGE", "outcome": "result_rejected",
            }
            assert len(state["tool_requests"]) == 1
        finally:
            await service.aclose()
            await connector.aclose()
