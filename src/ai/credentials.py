"""Resolve only dedicated provider credential references from the local environment."""

import os
import re
from collections.abc import Mapping
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr

from agent_bot.ai.errors import AiError

REFERENCE_PATTERN = re.compile(r"AGENT_BOT_PROVIDER_[A-Z0-9_]{1,100}\Z")


class CredentialResolver:
    """Keep secrets out of configuration tables and public response documents."""

    def __init__(self, env_file: Path | None = None, environ: Mapping[str, str] | None = None):
        self._path = env_file if env_file is not None else Path(".env")
        self._environ = os.environ if environ is None else environ

    def resolve(self, reference: str | None) -> SecretStr | None:
        if reference is None:
            return None
        if not REFERENCE_PATTERN.fullmatch(reference):
            raise AiError("AI_CREDENTIAL_REFERENCE_INVALID", status_code=422)
        values = dotenv_values(self._path, interpolate=False) if self._path.is_file() else {}
        value = self._environ.get(reference, values.get(reference))
        if not value or not value.strip() or any(char in value for char in "\r\n\x00"):
            raise AiError("AI_CREDENTIAL_MISSING", status_code=503)
        return SecretStr(value)

    def is_available(self, reference: str | None) -> bool:
        try:
            self.resolve(reference)
        except AiError:
            return False
        return True
