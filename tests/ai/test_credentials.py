import pytest

from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.errors import AiError


def test_credential_scope_and_precedence(tmp_path):
    path = tmp_path / "offline.env"
    path.write_text("AGENT_BOT_PROVIDER_TEST_KEY=file-value\n", encoding="utf-8")
    resolver = CredentialResolver(path, {"AGENT_BOT_PROVIDER_TEST_KEY": "env-value"})
    assert resolver.resolve("AGENT_BOT_PROVIDER_TEST_KEY").get_secret_value() == "env-value"
    assert resolver.resolve(None) is None
    assert "env-value" not in repr(resolver.resolve("AGENT_BOT_PROVIDER_TEST_KEY"))


def test_literal_dotenv_and_missing_credentials(tmp_path):
    path = tmp_path / "offline.env"
    path.write_text("AGENT_BOT_PROVIDER_TEST_KEY=${NOT_INTERPOLATED}\n", encoding="utf-8")
    resolver = CredentialResolver(path, {})
    assert (
        resolver.resolve("AGENT_BOT_PROVIDER_TEST_KEY").get_secret_value() == "${NOT_INTERPOLATED}"
    )
    for reference in ("AGENT_BOT_ADMIN_TOKEN", "AGENT_BOT_PROVIDER_MISSING"):
        with pytest.raises(AiError):
            resolver.resolve(reference)
        assert not resolver.is_available(reference)
