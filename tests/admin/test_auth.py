"""Administrator authentication is independent from chatbot credentials."""

import pytest
from fastapi import HTTPException


def test_admin_token_is_required_and_placeholders_are_rejected(monkeypatch, tmp_path):
    from agent_bot.admin.config import AdminConfig

    # 认证读取包含 .env，测试应在独立目录验证缺失凭据。
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_BOT_ADMIN_TOKEN", raising=False)
    with pytest.raises(ValueError):
        AdminConfig.from_environment()
    for token in ("short", "replace-with-a-new-admin-token", " ", "x" * 32 + "\n"):
        with pytest.raises(ValueError):
            AdminConfig(token=token)


def test_admin_token_repr_is_redacted(monkeypatch):
    from agent_bot.admin.config import AdminConfig

    token = "offline-admin-token-longer-than-thirty-two-characters"
    monkeypatch.setenv("AGENT_BOT_ADMIN_TOKEN", token)
    config = AdminConfig.from_environment()
    assert token not in repr(config)


@pytest.mark.parametrize("header", [None, "Basic abc", "Bearer wrong", "Bearer "])
def test_wrong_authentication_is_rejected(header):
    from agent_bot.admin.auth import require_token

    with pytest.raises(HTTPException) as failure:
        require_token(header, "offline-admin-token-longer-than-thirty-two-characters")
    assert failure.value.status_code == 401


def test_correct_authentication_is_accepted():
    from agent_bot.admin.auth import require_token

    token = "offline-admin-token-longer-than-thirty-two-characters"
    assert require_token(f"Bearer {token}", token) is None
