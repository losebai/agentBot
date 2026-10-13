"""验证启动诊断能指出跨层配置依赖，同时不回显秘密或未知异常上下文。"""

import pytest
from pydantic import BaseModel, ValidationError, model_validator

from agent_bot.cli_errors import describe_config_error
from agent_bot.config import RuntimeConfig
from agent_bot.settings import SettingsValidationError

ONEBOT_TOKEN = "offline-diagnostic-onebot-token-longer-than-32"
SESSION_SECRET = "offline-diagnostic-session-secret-longer-than-32"


@pytest.mark.parametrize(
    ("values", "fields", "reason"),
    [
        (
            {"chat_local_workflow_enabled": True, "chat_image_generation_enabled": True},
            ("CHAT_LOCAL_WORKFLOW_ENABLED", "CHAT_MCP_ENABLED"),
            "本地工作流需要开启 MCP",
        ),
        (
            {"chat_job_enabled": True},
            ("CHAT_JOB_ENABLED", "CHAT_MCP_ENABLED"),
            "后台任务需要开启 MCP",
        ),
    ],
)
def test_cross_layer_dependency_reports_static_fix_without_secret_warning(
    values: dict, fields: tuple[str, ...], reason: str,
) -> None:
    """静态 MCP 关闭导致动态功能失效时，提示修改启动文件并保留业务开关。"""
    with pytest.raises(ValidationError) as caught:
        RuntimeConfig(
            onebot_v11_access_token=ONEBOT_TOKEN,
            chat_session_secret=SESSION_SECRET,
            **values,
        )
    error = SettingsValidationError("settings snapshot failed validation")
    error.__cause__ = caught.value

    message = describe_config_error(error)

    assert all(field in message for field in fields)
    assert reason in message
    assert ".env" in message and "重启" in message
    assert "有效配置组合" in message
    assert "示例占位符" not in message
    assert ONEBOT_TOKEN not in message and SESSION_SECRET not in message


def test_equal_startup_secrets_reports_actual_constraint() -> None:
    """密钥相同应提示用途隔离，而不能笼统归因于示例占位符。"""
    with pytest.raises(ValidationError) as caught:
        RuntimeConfig(onebot_v11_access_token=ONEBOT_TOKEN, chat_session_secret=ONEBOT_TOKEN)

    message = describe_config_error(caught.value)

    assert "ONEBOT_V11_ACCESS_TOKEN" in message and "CHAT_SESSION_SECRET" in message
    assert "必须不同" in message
    assert "示例占位符" not in message
    assert ONEBOT_TOKEN not in message


def test_unknown_validator_context_is_not_disclosed() -> None:
    """未登记的校验器消息可能包含输入秘密，必须只显示通用错误。"""
    class PrivateConfig(BaseModel):
        """模拟不可信校验器，确保未知上下文始终不进入命令输出。"""

        @model_validator(mode="after")
        def fail(self) -> "PrivateConfig":
            """模拟带有凭据的底层错误。"""
            raise ValueError(ONEBOT_TOKEN)

    with pytest.raises(ValidationError) as caught:
        PrivateConfig()

    message = describe_config_error(caught.value)

    assert "配置组合" in message
    assert ONEBOT_TOKEN not in message
    assert "示例占位符" not in message
