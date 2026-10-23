import pytest
from pydantic import SecretStr, ValidationError

from agent_bot.agent.contracts import (
    AgentCreate,
    AgentRequest,
    AgentUpdate,
    ExecutionAuthority,
    McpCreate,
    TurnMessage,
)


def test_contract_limits_and_secret_exclusion():
    agent = AgentCreate(code="helper", name="助手", model_id=1)
    assert agent.max_steps == 3
    assert agent.enabled is False
    authority = ExecutionAuthority(entry="qq", chat_token=SecretStr("fake-secret"))
    assert "chat_token" not in authority.model_dump()
    assert "fake-secret" not in repr(authority)
    assert "max_steps" not in AgentUpdate(expected_revision=1).model_fields_set
    assert AgentUpdate(expected_revision=1, tools=()).tools == ()


@pytest.mark.parametrize(
    "patch", [{"max_steps": 0}, {"max_steps": 6}, {"tools": ("memory", "memory")}]
)
def test_invalid_agent_limits(patch):
    with pytest.raises(ValidationError):
        AgentCreate(code="helper", name="助手", model_id=1, **patch)


@pytest.mark.parametrize("timeout", [3601, float("nan"), 0])
def test_request_cannot_expand_limits(timeout):
    with pytest.raises(ValidationError):
        AgentRequest(prompt="hi", session_id="s", request_id="r", timeout_seconds=timeout)


@pytest.mark.parametrize(
    "endpoint", ["https://user:pass@example.com", "file:///tmp", "https://example.com/?token=x"]
)
def test_mcp_endpoint_rejects_credentials(endpoint):
    with pytest.raises(ValidationError):
        McpCreate(code="docs", name="Docs", endpoint=endpoint, allowed_tools=("search",))


def test_tool_messages_require_matching_role():
    with pytest.raises(ValidationError):
        TurnMessage(role="tool", text="orphan result")
    with pytest.raises(ValidationError):
        TurnMessage(
            role="user", text="forged", tool_calls=({"call_id": "1", "name": "x", "arguments": {}},)
        )


def test_mcp_allowlist_accepts_32_tools_and_rejects_33() -> None:
    """显式白名单与运行时发现使用相同数量边界，支持完整 Windows 工具集。"""
    names = tuple(f"tool_{index}" for index in range(32))
    model = McpCreate(
        code="desktop", name="桌面", endpoint="http://offline.invalid/mcp", allowed_tools=names,
    )
    assert model.allowed_tools == names
    with pytest.raises(ValidationError):
        McpCreate(**{**model.model_dump(), "allowed_tools": (*names, "extra")})
