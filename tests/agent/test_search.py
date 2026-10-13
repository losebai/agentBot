import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall, ToolResult
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.runtime import ToolRuntime
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


@pytest.mark.parametrize("entry", ["runtime", "call"])
async def test_missing_search_configuration_is_not_retryable(entry: str) -> None:
    """缺少搜索绑定属于配置拒绝，不能当作临时网络故障重复执行整轮请求。"""
    class Connector:
        @asynccontextmanager
        async def open(self, servers, *, remaining):
            """替代空远端工具集合，不允许测试连接真实服务。"""
            assert not servers
            yield SimpleNamespace(tools=(), warnings=(), call=None)

    with pytest.raises(AgentError) as failure:
        if entry == "call":
            await SearchTool(Connector(), None).call(
                ToolCall(call_id="1", name="web_search", arguments={"query": "示例"})
            )
        else:
            snapshot = SimpleNamespace(agent=None, mcps=(), search_mcp=None)
            request = AgentRequest(prompt="示例", session_id="s", request_id="r")
            authority = ExecutionAuthority(entry="qq", allowed_tools=("web_search",))
            async with ToolRuntime(Connector()).open(snapshot, request, authority):
                pass
    assert failure.value.code == "AGENT_SEARCH_UNAVAILABLE"
    assert failure.value.status_code == 409
