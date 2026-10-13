"""Directory row encoding, restricted to explicitly known models and columns."""

import json

from agent_bot.agent.contracts import (
    AgentCreate,
    AgentView,
    McpCreate,
    McpView,
    SkillCreate,
    SkillView,
)

CREATE = {"agent": AgentCreate, "skill": SkillCreate, "mcp": McpCreate}
VIEW = {"agent": AgentView, "skill": SkillView, "mcp": McpView}
JSON_FIELDS = {"tools", "options", "allowed_tools"}
RELATIONS = {"skill_ids": "skill", "mcp_ids": "mcp"}


def encode(values):
    return {
        key: json.dumps(value, ensure_ascii=False, allow_nan=False) if key in JSON_FIELDS else value
        for key, value in values.items()
    }


def decode(values):
    return {
        key: json.loads(value) if key in JSON_FIELDS and isinstance(value, str) else value
        for key, value in values.items()
    }
