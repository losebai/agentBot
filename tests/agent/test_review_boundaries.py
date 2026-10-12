import json
from types import SimpleNamespace

import pytest
from mcp import types
from pydantic import SecretStr

from agent_bot.agent.contracts import ExecutionAuthority, ToolCall, ToolDefinition
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.chat import ChatToolBridge
from agent_bot.agent.tools.read_url import ReadUrlTool
from agent_bot.agent.tools.schema import ToolSchema


async def test_full_memory_tag_catalog_is_preserved():
    catalog = json.dumps(
        {"tags": [{"scope": "user", "tag": "topic-" + str(i) + "x" * 50} for i in range(40)]}
    )
    description = "Memory instructions\n标签目录：\n" + catalog

    class Server:
        async def list_tools(self, token):
            return types.ListToolsResult(
                tools=[
                    types.Tool(
                        name="memory", description=description, inputSchema={"type": "object"}
                    )
                ]
            )

    tools = await ChatToolBridge(Server()).list_tools(
        ExecutionAuthority(entry="qq", allowed_tools=("memory",), chat_token=SecretStr("fake"))
    )
    assert tools[0].description.endswith(catalog)


async def test_read_url_json_escaping_keeps_pagination_bounded():
    class Reader:
        async def read(self, *args, **kwargs):
            return SimpleNamespace(
                url="https://example.com",
                title="",
                content_type="text/plain",
                content="\0" * 25000,
                links=(),
                truncated=False,
            )

    result = await ReadUrlTool(Reader()).call(
        ToolCall(
            call_id="1",
            name="read_url",
            arguments={"url": "https://example.com", "maxCharacters": 20000},
        )
    )
    value = json.loads(result.content)
    assert len(result.content) <= 32000 and value["hasMore"]
    assert value["nextOffset"] == len(value["content"]) and value["nextOffset"] > 0


def test_schema_expansion_has_a_work_budget():
    definitions = {"leaf": {"type": "string"}}
    for i in range(12):
        ref = "#/$defs/" + ("leaf" if i == 0 else str(i - 1))
        definitions[str(i)] = {"allOf": [{"$ref": ref}, {"$ref": ref}]}
    schema = {"type": "object", "$defs": definitions, "properties": {"x": {"$ref": "#/$defs/11"}}}
    with pytest.raises(AgentError):
        ToolSchema.validate_definition(
            ToolDefinition(name="test", source="mcp", input_schema=schema)
        )
