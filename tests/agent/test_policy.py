import pytest

from agent_bot.agent.contracts import ExecutionAuthority, ToolDefinition
from agent_bot.agent.errors import AgentError
from agent_bot.agent.prompting import FINAL_INSTRUCTION, AgentPromptBuilder
from agent_bot.agent.tools.policy import ToolPolicy
from agent_bot.agent.tools.schema import ToolSchema


def definition(schema=None):
    return ToolDefinition(
        name="memory",
        source="chat",
        input_schema=schema
        or {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
            "additionalProperties": False,
        },
    )


def test_skill_dependencies_never_grant_tools():
    with pytest.raises(AgentError) as failure:
        ToolPolicy.select(
            configured=("memory",),
            requested=("memory",),
            authority=ExecutionAuthority(entry="admin"),
            available=(definition(),),
        )
    assert failure.value.code == "AGENT_TOOL_UNAUTHORIZED"


def test_empty_selection_disables_tools():
    assert (
        ToolPolicy.select(
            configured=("memory",),
            requested=(),
            authority=ExecutionAuthority(entry="qq", allowed_tools=("memory",)),
            available=(definition(),),
        )
        == ()
    )


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "$ref": "https://example.invalid/schema"},
        {"type": "object", "$defs": {"x": {"$ref": "#/$defs/x"}}, "$ref": "#/$defs/x"},
        {"type": "object", "patternProperties": {"(a+)+": {"type": "string"}}},
        {"type": "object", "surprise": True},
    ],
)
def test_schema_rejects_remote_ref_without_network(schema):
    with pytest.raises(AgentError):
        ToolSchema.validate_definition(definition(schema))


def test_schema_validates_arguments_without_coercion():
    ToolSchema.validate_definition(definition())
    assert ToolSchema.validate_arguments(definition(), {"key": "hello"}) == {"key": "hello"}
    for args in [{"key": 4}, {}, {"key": "ok", "extra": 1}]:
        with pytest.raises(AgentError):
            ToolSchema.validate_arguments(definition(), args)


def test_prompt_size_boundaries():
    prompt = AgentPromptBuilder.build("角色", (), (), final_only=True)
    assert prompt.endswith(FINAL_INSTRUCTION)
    with pytest.raises(AgentError):
        AgentPromptBuilder.build("x" * 32001, (), ())
