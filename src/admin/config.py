"""Administrator bootstrap configuration, separate from bot runtime settings."""

import os
import re

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator


class AdminConfig(BaseModel):
    """Keep the administrator credential out of logs and runtime settings responses."""

    model_config = ConfigDict(hide_input_in_errors=True)
    token: SecretStr

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        """Require a non-placeholder URL-safe credential suitable for HTTP headers."""
        token = value.get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token) or token.startswith("replace-"):
            raise ValueError("administrator credential must be a new URL-safe token")
        return value

    @classmethod
    def from_environment(cls) -> "AdminConfig":
        """Read the bootstrap file, allowing an explicit environment value to take priority."""
        values = dotenv_values(".env", interpolate=False)
        token = os.environ.get("AGENT_BOT_ADMIN_TOKEN", values.get("AGENT_BOT_ADMIN_TOKEN"))
        if not token:
            raise ValueError("AGENT_BOT_ADMIN_TOKEN is required")
        return cls(token=token)
