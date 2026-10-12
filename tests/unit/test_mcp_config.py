from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_bot.config import PluginConfig


def make_config(**overrides: object) -> PluginConfig:
    values: dict[str, object] = {
        "onebot_v11_access_token": "onebot-token-with-at-least-thirty-two-characters",
        "dispatch_chat_session_secret": "session-secret-with-at-least-thirty-two-characters",
    }
    values.update(overrides)
    return PluginConfig(**values)


def test_mcp_is_disabled_by_default_with_bounded_settings() -> None:
    config = make_config()

    assert config.dispatch_chat_mcp_enabled is False
    assert config.dispatch_chat_mcp_mount_path == "/mcp"
    assert config.dispatch_chat_mcp_session_ttl_seconds <= 300
    assert config.dispatch_chat_mcp_max_sessions <= 10_000
    assert "dispatch-chat:*" in config.dispatch_chat_mcp_allowed_hosts


@pytest.mark.parametrize("path", ["mcp", "/", "/mcp?token=secret", "/mcp#fragment"])
def test_mcp_rejects_unsafe_mount_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        make_config(dispatch_chat_mcp_mount_path=path)


def test_mcp_rejects_empty_or_unbounded_host_allow_lists() -> None:
    with pytest.raises(ValidationError):
        make_config(dispatch_chat_mcp_allowed_hosts=[])
    with pytest.raises(ValidationError):
        make_config(dispatch_chat_mcp_allowed_hosts=[f"host-{index}:*" for index in range(21)])
