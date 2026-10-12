"""Dedicated MCP environment references, never database-stored secrets."""

import os
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr

from agent_bot.agent.contracts import MCP_REFERENCE
from agent_bot.agent.errors import AgentError


class McpCredentials:
    def __init__(self, env_file=None, environ=None):
        self.path = Path(env_file) if env_file is not None else Path(".env")
        self.environ = os.environ if environ is None else environ

    def resolve(self, reference):
        if reference is None:
            return None
        if not MCP_REFERENCE.fullmatch(reference):
            raise AgentError("AGENT_CREDENTIAL_REFERENCE_INVALID", status_code=422)
        values = dotenv_values(self.path, interpolate=False) if self.path.is_file() else {}
        value = self.environ.get(reference, values.get(reference))
        if not value or not value.strip() or any(c in value for c in "\r\n\0"):
            raise AgentError("AGENT_CREDENTIAL_MISSING", status_code=503)
        return SecretStr(value)

    def is_available(self, reference):
        try:
            self.resolve(reference)
            return True
        except AgentError:
            return False
