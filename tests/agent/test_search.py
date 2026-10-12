import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

from agent_bot.agent.contracts import ToolCall, ToolResult
from agent_bot.agent.tools.search import SearchTool


async def test_search_modes_and_result_limits():
    calls = []

    class Connector:
        @asynccontextmanager
        async def open(self, servers, *, remaining):
            yield self

        async def call(self, call):
            calls.append(call)
            return ToolResult(
                call_id=call.call_id,
                name=call.name,
                content=json.dumps(
                    {
                        "results": [
                            {"title": "Title", "url": "https://example.com", "content": "x" * 3000}
                            for _ in range(8)
                        ]
                    }
                ),
            )

    server = SimpleNamespace(search_tool="searxng_web_search", category="search")
    tool = SearchTool(Connector(), server)
    result = await tool.call(ToolCall(call_id="1", name="web_search", arguments={"query": "天气"}))
    data = json.loads(result.content)
    assert len(data["results"]) == 5
    assert sum(len(r["snippet"]) for r in data["results"]) <= 10000
    assert calls[0].arguments["num_results"] == 5
    assert calls[0].arguments["response_format"] == "json"
