"""真实业务服务、LangGraph、LangChain 和离线模型的集成验证。"""

import json
from contextlib import asynccontextmanager

import httpx
import pytest
from langsmith import Client

from agent.test_model_protocols import body, streamed
from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import (
    AgentCreate,
    AgentRequest,
    ExecutionAuthority,
    ToolDefinition,
    ToolResult,
)
from agent_bot.agent.errors import AgentError
from agent_bot.agent.records import AgentRunStore
from agent_bot.agent.service import AgentService
from agent_bot.agent.tools.runtime import BoundTools
from agent_bot.ai.contracts import ModelCreate, ProviderCreate, ProviderUpdate, TextRequest
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
        maximum_tool_calls=1,
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
        assert all(row[1] == "Bearer offline-key" for row in sent)
        ordinary = await plain.complete(
            TextRequest(model="local-model", prompt="hi", request_id="p", session_id="s")
        )
        assert ordinary.content == "你好\n\n世界"
        assert sent[-1][0].startswith("https://updated.invalid/")
        assert sent[-1][1] == "Bearer new-key"
        assert "tools" not in sent[-1][2]
    finally:
        await service.aclose()


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses"])
@pytest.mark.parametrize("repeated_markup", [False, True])
async def test_fallback_markup_correction_keeps_native_tools_and_truthful_status(
    agent_database, agent_params, seeded_model, protocol, repeated_markup,
):
    """真实 SDK 的备用响应污染可纠正为原生调用，纠正无效必须保留失败终态。"""
    catalog, primary, model = seeded_model
    await catalog.update_provider(
        primary.id, ProviderUpdate(expected_revision=primary.revision, protocol=protocol),
    )
    fallback = await catalog.create_provider(ProviderCreate(
        name="fallback", protocol=protocol, base_url="https://fallback.invalid/v1",
        credential="fallback-key", enabled=True,
    ))
    fallback_model = await catalog.create_model(ModelCreate(
        upstream_model="backup/id", provider_id=fallback.id, enabled=True,
    ))
    agents = AgentCatalog(agent_params, connect=agent_database.connect)
    await agents.create_agent(AgentCreate(
        code="helper", name="助手", model_id=model.id, fallback_model_id=fallback_model.id,
        enabled=True, tools=("lookup",),
    ))
    marker = "\uff5c\uff5cDSML\uff5c\uff5c"
    polluted = (f"继续查找下载入口。<{marker} calls>"
                f'<{marker} invoke name="lookup">'
                f"</{marker} invoke></{marker} calls>")
    sent = []
    primary_attempts = []

    async def handle(request):
        """仅替换 HTTP，保留供应商切换、SDK 解码和完整工具执行流程。"""
        payload = json.loads(request.content)
        if request.url.host == "original.invalid":
            primary_attempts.append(payload)
            return httpx.Response(503, json={"error": {"message": "offline failure"}})
        sent.append(payload)
        retry = len(sent) == 2
        tools_bound = len(sent) <= 2
        if tools_bound:
            assert payload.get("tools") and payload.get("tool_choice") != "none"
        if retry:
            assert "原始任务" in json.dumps(payload, ensure_ascii=False)
        malformed = len(sent) == 1 or (retry and repeated_markup)
        native_call = retry and not repeated_markup
        value = body(protocol, tool=native_call)
        if protocol == "openai-chat":
            message = value["choices"][0]["message"]
            if native_call:
                message["tool_calls"][0]["function"].update(name="lookup", arguments="{}")
        else:
            message = value["output"][0]["content"][0]
            if native_call:
                value["output"][1].update(name="lookup", arguments="{}")
        if malformed:
            message["content" if protocol == "openai-chat" else "text"] = polluted
        if not tools_bound:
            assert not payload.get("tools") and payload["tool_choice"] == "none"
            assert "verified facts" in json.dumps(payload)
        return httpx.Response(200, content=streamed(protocol, value))

    adapter = LangChainProvider(protocol, transport=httpx.MockTransport(handle))
    tools = LocalTools()
    records = AgentRunStore(agent_params, connect=agent_database.connect)
    service = AgentService(
        agents, records, CredentialResolver(), {protocol: adapter}, tools, maximum_tool_calls=1,
    )
    try:
        request = AgentRequest(
            agent_code="helper", prompt="下载客户端", request_id="r", session_id="s",
        )
        authority = ExecutionAuthority(entry="qq", allowed_tools=("lookup",))
        if repeated_markup:
            with pytest.raises(AgentError, match="AGENT_MODEL_RESPONSE_INVALID"):
                await service.complete(request, authority)
            assert tools.invoked == [] and len(sent) == 2
        else:
            result = await service.complete(request, authority)
            assert result.content == "已查询到结果" and result.record_saved
            assert tools.invoked == ["c1"] and len(sent) == 3
        assert primary_attempts
        run = (await records.list_runs()).items[0]
        assert run.status == ("failed" if repeated_markup else "completed")
        detail = await records.get_run(run.execution_id)
        if not repeated_markup:
            assert "DSML" not in detail.content
        steps = (await records.list_steps(run.execution_id)).items
        assert [step.summary for step in steps if step.kind == "warning"] == [
            "AI_UPSTREAM_UNAVAILABLE：local / exact/id 转交 fallback / backup/id",
            "AGENT_MODEL_MARKUP_RETRIED",
        ]
        assert service.active == tools.active == 0
    finally:
        await service.aclose()
