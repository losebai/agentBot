"""Stable web_search interface backed by one registered search MCP."""

import json

from agent_bot.agent.contracts import ToolCall, ToolDefinition, ToolResult
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.public_http import safe_url
from agent_bot.agent.tools.schema import ToolSchema
from agent_bot.services.request_deadline import remaining_timeout


class SearchTool:
    definition = ToolDefinition(
        name="web_search",
        source="builtin",
        description="搜索公开网页并返回来源。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 300},
                "numResults": {"type": "integer", "minimum": 1, "maximum": 5},
                "searchMode": {"type": "string", "enum": ["auto", "request", "playwright"]},
                "contextMaxCharacters": {"type": "integer", "minimum": 1000, "maximum": 10000},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )

    def __init__(self, connector, server):
        self.connector, self.server = connector, server

    async def call(self, call):
        args = ToolSchema.validate_arguments(self.definition, call.arguments)
        if self.server is None or self.server.category != "search":
            raise AgentError("AGENT_SEARCH_UNAVAILABLE", status_code=503)
        query = args["query"].strip()
        if not query:
            raise AgentError("AGENT_TOOL_ARGUMENTS_INVALID", status_code=422)
        limit, budget = args.get("numResults", 5), args.get("contextMaxCharacters", 10000)
        mapped = {"query": query}
        if self.server.search_tool == "searxng_web_search":
            mapped.update(num_results=limit, response_format="json", result_detail="compact")
        else:
            mapped["limit"] = limit
            if args.get("searchMode", "auto") != "auto":
                mapped["searchMode"] = args["searchMode"]
        async with self.connector.open(
            (self.server,), remaining=lambda: remaining_timeout(30)
        ) as connection:
            result = await connection.call(
                ToolCall(call_id=call.call_id, name=self.server.search_tool, arguments=mapped)
            )
        if result.is_error:
            return ToolResult(
                call_id=call.call_id, name=call.name, content=result.content, is_error=True
            )
        try:
            rows = json.loads(result.content)
            if isinstance(rows, dict):
                rows = rows["results"]
            if not isinstance(rows, list):
                raise ValueError
            output = []
            for item in rows:
                if len(output) >= limit or budget <= 0:
                    break
                if not isinstance(item, dict) or not safe_url(item.get("url")):
                    continue
                snippet = str(item.get("description", item.get("content", "")))[: min(2000, budget)]
                output.append(
                    {
                        "title": str(item.get("title", ""))[:300],
                        "url": item["url"],
                        "snippet": snippet,
                    }
                )
                budget -= len(snippet)
        except (ValueError, KeyError, TypeError) as error:
            raise AgentError("AGENT_SEARCH_RESPONSE_INVALID") from error
        return ToolResult(
            call_id=call.call_id,
            name=call.name,
            content=json.dumps({"query": query, "results": output}, ensure_ascii=False),
        )
