"""验证可选管理员密码及其与机器人凭据独立的访问边界。"""

import pytest
from fastapi import HTTPException


@pytest.mark.parametrize("configured", [None, ""])
def test_missing_or_empty_password_disables_authentication(monkeypatch, tmp_path, configured):
    """未配置或显式留空时允许启动，并标记为免登录。"""
    from agent_bot.admin.config import AdminConfig

    # 认证读取包含 .env，测试应在独立目录验证缺失凭据。
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENT_BOT_ADMIN_TOKEN", raising=False)
    if configured is not None:
        monkeypatch.setenv("AGENT_BOT_ADMIN_TOKEN", configured)
    assert AdminConfig.from_environment().token.get_secret_value() == ""


def test_invalid_passwords_are_rejected():
    """拒绝过短、超长、占位符和不适合请求头的密码，防止误开启免登录。"""
    from agent_bot.admin.config import AdminConfig

    for token in (
        "short", "replace-with-a-new-admin-token", " ", "x" * 257,
        "x" * 32 + "\n", "password\t", "password with space", "中文密码123456",
    ):
        with pytest.raises(ValueError):
            AdminConfig(token=token)


@pytest.mark.parametrize("password", ["Easy!123", "x" * 256, "replace-my-password"])
def test_custom_password_is_accepted(password):
    """允许用户记忆的密码及常见符号，不再要求三十二位随机令牌。"""
    from agent_bot.admin.config import AdminConfig

    assert AdminConfig(token=password).token.get_secret_value() == password


def test_empty_environment_password_overrides_file(monkeypatch, tmp_path):
    """环境变量显式留空时关闭密码，不能回退到文件中的旧凭据。"""
    from agent_bot.admin.config import AdminConfig

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("AGENT_BOT_ADMIN_TOKEN=Easy!123\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_BOT_ADMIN_TOKEN", "")
    assert AdminConfig.from_environment().token.get_secret_value() == ""


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


@pytest.mark.parametrize("header", [None, "Bearer old-password", "Basic ignored"])
def test_unconfigured_password_allows_management(header):
    """免登录模式不要求认证头，也不受浏览器残留凭据影响。"""
    from agent_bot.admin.auth import require_token

    assert require_token(header, "") is None
