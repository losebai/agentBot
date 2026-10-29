"""命名 Agent 直接使用其远程服务绑定，QQ 不再叠加全局 MCP 名单。"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.agent.contracts import (
    AgentCreate,
    AgentRequest,
    AgentUpdate,
    ExecutionAuthority,
    McpCreate,
    ToolDefinition,
    ToolResult,
)
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.runtime import ToolRuntime
from agent_bot.ai.routing import RoutingAiClient
from agent_bot.models import LlmRequestOptions

from .test_service import service_for


class RemoteConnector:
    """记录真实运行时连接的服务，只替换远端连接，不执行网络请求。"""

    def __init__(self):
        """保存连接与调用记录，以区分模型未选择和服务未传入。"""
        self.opened = []
        self.called = []

    @asynccontextmanager
    async def open(self, servers, **kwargs):
        """有服务时暴露一个离线工具，工具授权仍由真实 ToolRuntime 校验。"""
        self.opened.append(tuple(server.code for server in servers))
        yield SimpleNamespace(
            tools=(ToolDefinition(name="lookup", source="mcp",
                                  input_schema={"type": "object"}),) if servers else (),
            warnings=(), call=self.call,
        )

    async def call(self, call):
        """返回固定文本，避免调用付费模型或真实表情包服务。"""
        self.called.append(call.name)
        return ToolResult(call_id=call.call_id, name=call.name, content="表情包搜索结果")


@pytest.fixture
async def remote_agent(agent_database, agent_params, seeded_model):
    """准备绑定 memecub 的 Agent 和一个未绑定的全局服务。"""
    connector = RemoteConnector()
    service = service_for(agent_database, agent_params, tools=ToolRuntime(connector))
    meme = await service.catalog.create_mcp(McpCreate(
        code="memecub", name="表情包", endpoint="https://memecub.invalid/mcp",
        enabled=True, allowed_tools=("lookup",),
    ))
    await service.catalog.create_mcp(McpCreate(
        code="other", name="其他服务", endpoint="https://other.invalid/mcp",
        enabled=True, allowed_tools=("lookup",),
    ))
    await service.catalog.create_agent(AgentCreate(
        code="helper", name="助手", model_id=seeded_model[2].id, enabled=True,
        mcp_ids=(meme.id,), tools=("memory",),
    ))
    return service, connector


@pytest.mark.parametrize("global_services", [(), ("other",), ("laya",)])
async def test_named_agent_uses_bound_services_without_global_filter(remote_agent, global_services):
    """全局列表为空、不同或含旧服务时，都不能过滤命名 Agent 的有效绑定。"""
    service, connector = remote_agent
    client = RoutingAiClient(
        AsyncMock(), agent_service=service, agent_code="helper", timeout_seconds=30,
        default_options=LlmRequestOptions(), mcp_services=global_services,
    )
    answer = await client.complete(model="unused", prompt="来个表情包", messages=(),
                                   session_id="s", request_id="r", use_web_search=False)
    assert answer == "最终回答"
    assert connector.opened == [("memecub",)]
    assert connector.called == ["lookup"]
    # 命名 Agent 的远程授权不扩大 QQ 本地工具的会话授权。
    assert [tool.name for tool in service.providers["openai-chat"].calls[0].tools] == ["lookup"]


async def test_unnamed_chat_keeps_global_remote_services(remote_agent):
    """未指定 Agent 时继续使用全局列表，不能借用其他 Agent 的远程服务。"""
    service, connector = remote_agent
    client = RoutingAiClient(
        AsyncMock(), agent_service=service, timeout_seconds=30,
        default_options=LlmRequestOptions(), mcp_services=("other",),
    )
    await client.complete(model="local-model", prompt="搜索", messages=(), session_id="s",
                          request_id="r", use_web_search=False)
    assert connector.opened == [("other",)]
    assert connector.called == ["lookup"]


async def test_named_agent_without_bindings_does_not_use_global_services(remote_agent):
    """空绑定就是不使用远程服务，不能自动借用全局服务或其他 Agent 的绑定。"""
    service, connector = remote_agent
    agent = (await service.catalog.list_agents()).items[0]
    await service.catalog.update_agent(agent.id, AgentUpdate(
        expected_revision=agent.revision, mcp_ids=(),
    ))
    client = RoutingAiClient(
        AsyncMock(), agent_service=service, agent_code="helper", timeout_seconds=30,
        default_options=LlmRequestOptions(), mcp_services=("other",),
    )
    await client.complete(model="unused", prompt="你好", messages=(), session_id="s",
                          request_id="r", use_web_search=False)
    assert connector.opened == [()]
    assert connector.called == []


async def test_completion_notice_still_disables_bound_remote_services(remote_agent):
    """完成通知显式关闭所有工具，即使 Agent 已关联远程服务也不能重新连接。"""
    service, connector = remote_agent
    client = RoutingAiClient(
        AsyncMock(), agent_service=service, agent_code="helper", timeout_seconds=30,
        default_options=LlmRequestOptions(), mcp_services=("other",),
    )
    await client.complete(model="unused", prompt="图片已发送", messages=(), session_id="s",
                          request_id="notice", use_web_search=False, allow_tools=False)
    assert connector.opened == connector.called == []
    assert service.providers["openai-chat"].calls[0].tools == ()


async def test_explicit_request_cannot_add_unbound_remote_service(remote_agent):
    """显式服务请求仍须属于 Agent 绑定，去除全局过滤不允许模型越权加服务。"""
    service, connector = remote_agent
    with pytest.raises(AgentError, match="AGENT_TOOL_UNAUTHORIZED"):
        await service.complete(AgentRequest(
            agent_code="helper", prompt="搜索", session_id="s", request_id="r",
            mcp_services=("other",),
        ), ExecutionAuthority(entry="qq", allowed_mcp_services=("other",)))
    assert connector.opened == []


async def test_unnamed_request_still_requires_entry_remote_authorization(remote_agent):
    """未命名的内部请求不能借继承路径绕过入口授权。"""
    service, connector = remote_agent
    with pytest.raises(AgentError, match="AGENT_TOOL_UNAUTHORIZED"):
        await service.complete(AgentRequest(
            model="local-model", prompt="搜索", session_id="s", request_id="r",
            mcp_services=("other",),
        ), ExecutionAuthority(entry="qq"))
    assert connector.opened == []
