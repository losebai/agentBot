"""知识工具真实检索结果和命名 Agent 授权边界。"""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.knowledge import KnowledgeSearchTool
from agent_bot.agent.tools.runtime import ToolRuntime
from agent_bot.database import database_connector, initialize_database
from agent_bot.knowledge.contracts import KnowledgeCreate
from agent_bot.knowledge.store import KnowledgeStore
from agent_bot.settings import DatabaseParams


@pytest.fixture
def tool_store(tmp_path):
    """模型替身只替换生成，不模拟知识查询或权限判断。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "tool.db"))
    initialize_database(params)
    return KnowledgeStore(params, connect=database_connector(params))


async def test_tool_result_has_real_sources_and_bounded_content(tool_store):
    """工具结果带实际 ID 与正文截断状态，不能返回虚构来源。"""
    item = await tool_store.create(
        KnowledgeCreate(title="规则", content="文" * 2200, tags=["部署"])
    )
    tool = KnowledgeSearchTool(tool_store)
    result = await tool.call(
        ToolCall(call_id="1", name="knowledge_search", arguments={"tags": ["部署"]})
    )
    body = json.loads(result.content)
    assert body["records"][0]["id"] == item.id
    assert body["records"][0]["title"] == "规则"
    assert len(body["records"][0]["content"]) == 2000
    assert body["records"][0]["truncated"] is True
    assert not result.is_error
    empty = await tool.call(
        ToolCall(call_id="2", name="knowledge_search", arguments={"query": "无关"})
    )
    assert json.loads(empty.content)["records"] == []
    with pytest.raises(AgentError):
        await tool.call(ToolCall(call_id="3", name="knowledge_search", arguments={"query": " "}))


class EmptyConnector:
    """无外部 MCP 的实际授权测试连接。"""

    @asynccontextmanager
    async def open(self, *_args, **_kwargs):
        """不连接网络，仅表示此次没有关联远程工具。"""
        yield SimpleNamespace(tools=(), warnings=())


async def test_knowledge_tool_requires_agent_and_entry_authorization(tool_store):
    """入口权限和预设权限任一缺失，都不能调用知识工具。"""
    runtime = ToolRuntime(EmptyConnector(), knowledge_store=tool_store)
    request = AgentRequest(
        agent_code="helper", prompt="问题", session_id="admin_session", request_id="r1"
    )
    snapshot = SimpleNamespace(
        mcps=(), search_mcp=None, agent=SimpleNamespace(tools=("knowledge_search",))
    )
    authority = ExecutionAuthority(entry="admin", allowed_tools=("knowledge_search",))
    async with runtime.open(snapshot, request, authority) as tools:
        assert [tool.name for tool in tools.definitions] == ["knowledge_search"]
    async with runtime.open(snapshot, request, ExecutionAuthority(entry="admin")) as tools:
        assert not tools.definitions
        with pytest.raises(AgentError):
            await tools.call(
                ToolCall(call_id="1", name="knowledge_search", arguments={"query": "规则"})
            )


async def test_unconfigured_agent_does_not_query_missing_knowledge_table(tmp_path):
    """老库缺知识表不阻止未启用知识工具的 Agent 打开执行。"""
    path = tmp_path / "old.db"
    import sqlite3

    sqlite3.connect(path).close()
    params = DatabaseParams(backend="sqlite", sqlite_path=str(path))
    runtime = ToolRuntime(
        EmptyConnector(), knowledge_store=KnowledgeStore(params, connect=database_connector(params))
    )
    snapshot = SimpleNamespace(mcps=(), search_mcp=None, agent=SimpleNamespace(tools=()))
    request = AgentRequest(
        agent_code="helper", prompt="问题", session_id="admin_session", request_id="r2"
    )
    async with runtime.open(
        snapshot, request, ExecutionAuthority(entry="admin", allowed_tools=("knowledge_search",))
    ) as tools:
        assert not tools.definitions
    result = await KnowledgeSearchTool(runtime.knowledge_store).call(
        ToolCall(call_id="1", name="knowledge_search", arguments={"query": "规则"})
    )
    assert result.is_error
    assert json.loads(result.content)["code"] == "KNOWLEDGE_NOT_INITIALIZED"
