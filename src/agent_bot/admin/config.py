"""读取独立于机器人运行配置的可选管理员密码。"""

import os
import re

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, SecretStr, field_validator


class AdminConfig(BaseModel):
    """密码留空时免登录，非空密码不得进入日志或运行配置响应。"""

    model_config = ConfigDict(hide_input_in_errors=True)
    token: SecretStr = SecretStr("")

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: SecretStr) -> SecretStr:
        """允许自定义密码；保留请求头字符限制，拒绝把无效配置当作免登录。"""
        token = value.get_secret_value()
        if token and (
            not re.fullmatch(r"[!-~]{8,256}", token)
            or token == "replace-with-a-new-admin-token"
        ):
            raise ValueError("administrator password must contain 8-256 visible ASCII characters")
        return value

    @classmethod
    def from_environment(cls) -> "AdminConfig":
        """环境变量优先于启动文件，缺失或显式留空均关闭密码校验。"""
        values = dotenv_values(".env", interpolate=False)
        token = os.environ.get("AGENT_BOT_ADMIN_TOKEN", values.get("AGENT_BOT_ADMIN_TOKEN"))
        return cls(token=token or "")
