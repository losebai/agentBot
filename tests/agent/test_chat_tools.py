import json

import pytest
from pydantic import SecretStr

from agent_bot.agent.contracts import ExecutionAuthority, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.chat import ChatToolBridge
from agent_bot.integrations.mcp import ChatMcpServer
from agent_bot.services.chat_tools import ChatToolContext, ChatToolDefinition, ChatToolRegistry
from agent_bot.services.tool_sessions import ToolSessionStore


async def test_revoked_token_prevents_second_call():
    invoked = []

    async def memory(args, context):
        invoked.append(context.session_id)
        return {"session": context.session_id, "user": context.user_id}

    registry = ChatToolRegistry(
        [
            ChatToolDefinition(
                name="memory",
                description="Memory",
                input_schema={"type": "object", "additionalProperties": False},
                handler=memory,
            )
        ]
    )
    store = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    token = await store.create(
        ChatToolContext(session_id="group:1", user_id="u1", allowed_tools=frozenset({"memory"}))
    )
    bridge = ChatToolBridge(
        ChatMcpServer(store=store, registry=registry, allowed_hosts=["localhost:*"])
    )
    authority = ExecutionAuthority(
        entry="qq", allowed_tools=("memory",), chat_token=SecretStr(token)
    )
    result = await bridge.call(ToolCall(call_id="one", name="memory", arguments={}), authority)
    assert json.loads(result.content) == {"session": "group:1", "user": "u1"}
    await store.revoke(token)
    with pytest.raises(AgentError) as error:
        await bridge.call(ToolCall(call_id="two", name="memory", arguments={}), authority)
    assert error.value.code == "AGENT_TOOL_UNAUTHORIZED"
    assert invoked == ["group:1"]


async def test_gui_cannot_forge_qq_scope():
    bridge = ChatToolBridge(None)
    with pytest.raises(AgentError) as failure:
        await bridge.call(
            ToolCall(call_id="1", name="mention_member", arguments={}),
            ExecutionAuthority(
                entry="admin", allowed_tools=("mention_member",), chat_token=SecretStr("fake-token")
            ),
        )
    assert failure.value.status_code == 403
