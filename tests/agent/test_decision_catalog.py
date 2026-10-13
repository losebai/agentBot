"""决策 MCP 目录快照的授权边界，使用内存数据库。"""

import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import McpCreate
from agent_bot.agent.errors import AgentError


@pytest.mark.parametrize(
    "changes",
    [
        {"enabled": False},
        {"allowed_tools": ("another",)},
        {"category": "search", "search_tool": "laya_predict"},
    ],
)
async def test_decision_catalog_rejects_unavailable_service(agent_database, agent_params, changes):
    """已禁用、未授权固定工具、搜索专用服务都不能参与决策。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    await catalog.create_mcp(
        McpCreate(
            **{
                "name": "决策",
                "code": "decision",
                "endpoint": "http://localhost:8000/mcp",
                "enabled": True,
                "allowed_tools": ("laya_predict",),
                **changes,
            }
        )
    )
    with pytest.raises(AgentError):
        await catalog.decision_mcp("decision")


async def test_decision_snapshot_narrows_wildcard_without_mutating_catalog(
    agent_database, agent_params
):
    """宽目录授权只在本次快照收紧，配置本身不被决策路径修改。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    created = await catalog.create_mcp(
        McpCreate(
            name="决策",
            code="decision",
            endpoint="http://localhost:8000/mcp",
            enabled=True,
            allowed_tools=("*",),
            required=False,
        )
    )
    snapshot = await catalog.decision_mcp("decision")
    assert snapshot.allowed_tools == ("laya_predict",) and snapshot.required
    assert snapshot.revision == created.revision
    saved = await catalog.get_mcp(created.id)
    assert saved.allowed_tools == ("*",) and not saved.required
