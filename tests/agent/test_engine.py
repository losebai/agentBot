"""使用受控模型和工具验证 Agent 循环、整批参数和终态边界。"""
from contextlib import asynccontextmanager

import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import (
    AgentCreate,
    AgentRequest,
    ExecutionAuthority,
    ModelEvent,
    ModelTurn,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent_bot.agent.records import AgentRunStore
from agent_bot.agent.service import AgentService
from agent_bot.ai.credentials import CredentialResolver


class Provider:
    def __init__(self):
        self.calls = []

    def validate_options(self, options):
        pass

    async def stream_turn(self, call):
        self.calls.append(call)
        if call.final_only:
            yield ModelEvent(kind="text_delta", delta="最终回答")
            yield ModelEvent(kind="turn_end", turn=ModelTurn(content="最终回答", model="actual"))
        else:
            yield ModelEvent(kind="text_delta", delta="中间草稿")
            yield ModelEvent(
                kind="turn_end",
                turn=ModelTurn(
                    content="中间草稿",
                    model="actual",
                    calls=(ToolCall(call_id=f"c{len(self.calls)}", name="lookup", arguments={}),),
                ),
            )

    async def aclose(self):
        pass


class Tools:
    """测试工具端口支持媒体上下文，并复用生产参数校验。"""
    definitions = (ToolDefinition(name="lookup", source="mcp", input_schema={"type": "object"}),)
    warnings = ()

    def __init__(self):
        self.invoked, self.active = [], 0

    @asynccontextmanager
    async def open(self, snapshot, request, authority, **media_context):
        """接受真实运行时新增的私有媒体上下文。"""
        self.active += 1
        try:
            yield self
        finally:
            self.active -= 1

    async def call(self, call):
        self.invoked.append(call.call_id)
        return ToolResult(call_id=call.call_id, name=call.name, content="tool facts")

    def validate(self, call):
        """替身也执行原通用 schema 校验，不能绕过整批预检。"""
        from agent_bot.agent.tools.schema import ToolSchema

        ToolSchema.validate_arguments(
            next(t for t in self.definitions if t.name == call.name), call.arguments,
        )


@pytest.mark.parametrize("rounds", [1, 3, 5])
async def test_rounds_1_3_5_then_final_only(agent_database, agent_params, seeded_model, rounds):
    _, _, model = seeded_model
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    await catalog.create_agent(
        AgentCreate(
            code="helper",
            name="helper",
            model_id=model.id,
            max_steps=rounds,
            tools=("lookup",),
            enabled=True,
        )
    )
    provider, tools = Provider(), Tools()
    records = AgentRunStore(agent_params, connect=agent_database.connect)
    service = AgentService(
        catalog, records, CredentialResolver(), {"openai-chat": provider}, tools
    )
    result = await service.complete(
        AgentRequest(agent_code="helper", prompt="hello", session_id="s", request_id="r"),
        ExecutionAuthority(entry="admin", allowed_tools=("lookup",)),
    )
    assert result.content == "最终回答"
    assert len(provider.calls) == rounds + 1
    assert provider.calls[-1].final_only and provider.calls[-1].tools == ()
    assert len(tools.invoked) == rounds and tools.active == 0
    assert result.record_saved
    saved = await records.get_run(result.execution_id)
    assert saved.status == "completed" and saved.content == "最终回答"


async def test_authorization_failure_precedes_all_side_effects(
    agent_database, agent_params, seeded_model
):
    _, _, model = seeded_model
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    provider, tools = Provider(), Tools()

    async def malicious(call):
        yield ModelEvent(
            kind="turn_end",
            turn=ModelTurn(
                model="x",
                calls=(
                    ToolCall(call_id="good", name="lookup", arguments={}),
                    ToolCall(call_id="bad", name="unknown", arguments={}),
                ),
            ),
        )

    provider.stream_turn = malicious
    service = AgentService(
        catalog,
        AgentRunStore(agent_params, connect=agent_database.connect),
        CredentialResolver(),
        {"openai-chat": provider},
        tools,
    )
    from agent_bot.agent.errors import AgentError

    with pytest.raises(AgentError):
        await service.complete(
            AgentRequest(model=model.alias, prompt="hi", session_id="s", request_id="r"),
            ExecutionAuthority(entry="admin", allowed_tools=("lookup",)),
        )
    assert tools.invoked == [] and tools.active == 0
