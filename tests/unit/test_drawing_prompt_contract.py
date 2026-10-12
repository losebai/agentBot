"""确认绘图提示词说明通过 MCP 工具契约交付给模型。"""

from agent_bot.integrations.mcp import ChatMcpServer
from agent_bot.services.chat_tools import ChatToolContext, build_chat_tool_registry
from agent_bot.services.tool_sessions import ToolSessionStore


async def test_mcp_exposes_structured_drawing_prompt_guidance() -> None:
    """模型收到正负提示词用途说明，同时保留长度限制和原有参数契约。"""
    store = ToolSessionStore(ttl_seconds=30, max_sessions=1)
    token = await store.create(
        ChatToolContext(
            session_id="private:1",
            user_id="1",
            allowed_tools=frozenset({"local_workflow_generate_image"}),
        )
    )
    server = ChatMcpServer(
        store=store,
        registry=build_chat_tool_registry(),
        allowed_hosts=["localhost:*"],
    )

    tools = await server.list_tools(token)

    assert [tool.name for tool in tools.tools] == ["local_workflow_generate_image"]
    schema = tools.tools[0].input_schema
    positive = schema["properties"]["positive_prompt"]
    negative = schema["properties"]["negative_prompt"]
    for dimension in ("主体与风格", "外貌", "服饰", "姿势与焦点", "构图", "环境"):
        assert dimension in positive.get("description", "")
    assert "用户" in positive["description"]
    assert "排除" in negative.get("description", "")
    assert "冲突" in negative["description"]
    assert positive["maxLength"] == negative["maxLength"] == 2000
    assert negative["default"] == ""
    assert set(schema["required"]) == {"start_reply", "workflow_key", "positive_prompt"}
