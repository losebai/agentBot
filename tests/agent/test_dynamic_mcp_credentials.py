"""MCP 密钥在目录中独立维护且不公开。"""

import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import McpCreate, McpUpdate
from agent_bot.agent.mcp.credentials import McpCredentials
from agent_bot.ai.errors import AiError


async def test_mcp_secret_update_and_clear(agent_database, agent_params):
    """两个服务保持各自凭据，编辑超时保留密钥，明确清除后无需鉴权。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    first = await catalog.create_mcp(
        McpCreate(
            name="甲",
            code="one",
            endpoint="http://localhost:9001/mcp",
            allowed_tools=("lookup",),
            credential="first-secret",
        )
    )
    second = await catalog.create_mcp(
        McpCreate(
            name="乙",
            code="two",
            endpoint="http://localhost:9002/mcp",
            allowed_tools=("weather",),
            credential="second-secret",
        )
    )
    assert McpCredentials().resolve(first).get_secret_value() == "first-secret"
    assert McpCredentials().resolve(second).get_secret_value() == "second-secret"
    assert "first-secret" not in first.model_dump_json()
    assert "credential" not in first.model_dump()
    updated = await catalog.update_mcp(first.id, McpUpdate(expected_revision=1, timeout_seconds=20))
    assert McpCredentials().resolve(updated).get_secret_value() == "first-secret"
    cleared = await catalog.update_mcp(first.id, McpUpdate(expected_revision=2, credential=None))
    assert McpCredentials().resolve(cleared) is None


async def test_unmigrated_mcp_schema_requires_migration(agent_database, agent_params):
    """缺少凭据列时拒绝读取服务，避免旧库静默匿名连接。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    await catalog.create_mcp(
        McpCreate(
            name="旧服务",
            code="old",
            endpoint="http://localhost:9000/mcp",
            allowed_tools=("lookup",),
        )
    )
    agent_database.connection.execute("ALTER TABLE agent_bot_mcp DROP COLUMN credential")
    agent_database.connection.execute("ALTER TABLE agent_bot_mcp DROP COLUMN credential_required")
    agent_database.connection.commit()
    with pytest.raises(AiError, match="AI_NOT_INITIALIZED"):
        await catalog.list_mcps()
