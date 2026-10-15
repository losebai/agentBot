"""QQ 媒体工具保持原令牌和参数校验边界。"""

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from agent_bot.agent.contracts import ExecutionAuthority, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.chat import ChatToolBridge
from agent_bot.agent.tools.schema import ToolSchema
from agent_bot.integrations.mcp import ChatMcpServer
from agent_bot.services.chat_tools import ChatToolContext, build_chat_tool_registry
from agent_bot.services.tool_sessions import ToolSessionStore


async def test_media_schema_and_revoked_qq_token(monkeypatch: pytest.MonkeyPatch):
    """本地 schema 保留参数校验，撤销会话返回过期错误且不执行媒体工具。"""
    store = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    names = frozenset({"local_workflow_generate_image", "tts_generate", "local_workflow_cancel"})
    token = await store.create(ChatToolContext(session_id="g", user_id="u", allowed_tools=names))
    server = ChatMcpServer(
        store=store, registry=build_chat_tool_registry(), allowed_hosts=["localhost:*"]
    )
    call_tool = AsyncMock(wraps=server.call_tool)
    monkeypatch.setattr(server, "call_tool", call_tool)
    bridge = ChatToolBridge(server)
    authority = ExecutionAuthority(
        entry="qq", allowed_tools=tuple(names), chat_token=SecretStr(token)
    )
    definitions = await bridge.list_tools(authority)
    assert {tool.name for tool in definitions} == names
    for tool in definitions:
        ToolSchema.validate_definition(tool)
    with pytest.raises(AgentError, match="AGENT_TOOL_ARGUMENTS_INVALID"):
        await bridge.call(
            ToolCall(call_id="1", name="local_workflow_cancel", arguments={"task_id": "../path"}),
            authority,
        )
    await store.revoke(token)
    with pytest.raises(AgentError, match="AGENT_TOOL_SESSION_EXPIRED") as error:
        await bridge.call(
            ToolCall(call_id="2", name="tts_generate", arguments={"text": "你好"}), authority
        )
    assert error.value.status_code == 403
    call_tool.assert_not_awaited()


def test_local_projection_does_not_relax_remote_schema():
    """只允许登记过的本地工具剥离 pattern，未知工具保留原约束并拒绝。"""
    from agent_bot.agent.tools.local_media import project_local_schema

    schema = {"type": "object", "properties": {"task_id": {"type": "string", "pattern": "safe"}}}
    assert (
        "pattern"
        not in project_local_schema("local_workflow_cancel", schema)["properties"]["task_id"]
    )
    assert project_local_schema("remote", schema) == schema
    assert "pattern" in schema["properties"]["task_id"]
