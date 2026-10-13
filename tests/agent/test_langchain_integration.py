"""真实业务服务、LangGraph、LangChain 和离线模型的集成验证。"""

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from langsmith import Client

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolDefinition, ToolResult
from agent_bot.agent.records import AgentRunStore
from agent_bot.agent.service import AgentService
from agent_bot.agent.tools.runtime import BoundTools
from agent_bot.ai.contracts import ProviderUpdate, TextRequest
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.providers.langchain import LangChainProvider
from agent_bot.ai.service import AiService
from agent_bot.ai.sessions import AiSessionStore
from ai.test_providers import response


class LocalTools:
    """有明确生命周期的离线业务工具。"""

    def __init__(self):
        """记录执行次数与活动作用域。"""
        self.invoked, self.active = [], 0

    @asynccontextmanager
    async def open(self, *args, **kwargs):
        """复用实际 BoundTools 权限及参数校验。"""
        self.active += 1
        try:
            yield BoundTools(
                (ToolDefinition(name="lookup", source="builtin", input_schema={"type": "object"}),),
                {"lookup": self.call},
            )
        finally:
            self.active -= 1

    async def call(self, call):
        """工具结果必须在后续模型请求中出现。"""
        self.invoked.append(call.call_id)
        return ToolResult(call_id=call.call_id, name=call.name, content="verified facts")


async def test_plain_and_graph_agent_keep_snapshot_and_disable_external_tracing(
    agent_database,
    agent_params,
    seeded_model,
    monkeypatch,
):
    """动态配置只影响下次请求，环境 tracing 开关不能外传当前消息。"""
    catalog, provider_row, _ = seeded_model
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "offline-secret")

    def forbid_trace(*args, **kwargs):
        """任何创建外部跟踪记录都是业务数据外传。"""
        pytest.fail("unexpected external trace")

    monkeypatch.setattr(Client, "create_run", forbid_trace)
    monkeypatch.setattr(Client, "update_run", forbid_trace)
    sent = []

    async def handle(request):
        """第一轮期间修改数据库，后续轮仍应使用旧快照。"""
        payload = json.loads(request.content)
        sent.append((str(request.url), request.headers.get("authorization"), payload))
        if not payload["stream"]:
            return httpx.Response(200, json=response("openai-chat"))
        if len(sent) == 1:
            await catalog.update_provider(
                provider_row.id,
                ProviderUpdate(
                    expected_revision=1, base_url="https://updated.invalid/v1", credential="new-key"
                ),
            )
            delta = {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "tool-1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ]
            }
            reason = "tool_calls"
        else:
            assert payload["messages"][-1]["content"] == "verified facts"
            delta, reason = {"content": "最终答案"}, "stop"
        values = [
            {"model": "exact/id", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"choices": [{"index": 0, "delta": {}, "finish_reason": reason}]},
        ]
        raw = "".join("data: " + json.dumps(v) + "\n\n" for v in values) + "data: [DONE]\n\n"
        return httpx.Response(200, content=raw)

    adapter = LangChainProvider("openai-chat", transport=httpx.MockTransport(handle))
    tools = LocalTools()
    service = AgentService(
        AgentCatalog(agent_params, connect=agent_database.connect),
        AgentRunStore(agent_params, connect=agent_database.connect),
        CredentialResolver(),
        {"openai-chat": adapter},
        tools,
    )
    plain = AiService(
        catalog,
        AiSessionStore(agent_params, connect=agent_database.connect),
        CredentialResolver(),
        {"openai-chat": adapter},
    )
    try:
        result = await service.complete(
            AgentRequest(
                model="local-model", prompt="查资料", request_id="r", session_id="s", max_steps=1
            ),
            ExecutionAuthority(entry="admin", allowed_tools=("lookup",)),
        )
        assert result.content == "最终答案" and result.record_saved
        assert tools.invoked == ["tool-1"] and tools.active == 0
        assert len(sent) == 2 and all(
            row[0].startswith("https://original.invalid/") for row in sent
        )
        assert all(row[1] is None for row in sent)
        ordinary = await plain.complete(
            TextRequest(model="local-model", prompt="hi", request_id="p", session_id="s")
        )
        assert ordinary.content == "你好\n\n世界"
        assert sent[-1][0].startswith("https://updated.invalid/")
        assert sent[-1][1] == "Bearer new-key"
        assert "tools" not in sent[-1][2]
    finally:
        await service.aclose()
