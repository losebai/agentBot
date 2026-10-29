"""本机目录和终端工具的严格参数及固定工具名称。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

HOST_TOOLS = frozenset({"ls", "bash"})


class HostArguments(BaseModel):
    """不接受模型伪造用户、环境变量或身份字段。"""

    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class LsArguments(HostArguments):
    """按原生目录顺序分页，不递归或读取文件正文。"""

    path: str = Field(default=".", min_length=1, max_length=4096)
    include_hidden: bool = False
    offset: int = Field(default=0, ge=0, le=100000)
    limit: int = Field(default=100, ge=1, le=200)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """路径按原文交给操作系统，拒绝空白及无法传入文件 API 的空字符。"""
        if not value.strip() or "\x00" in value:
            raise ValueError("invalid path")
        return value


class BashArguments(HostArguments):
    """每次命令独立执行，工作目录和执行期限均显式提供。"""

    command: str = Field(min_length=1, max_length=16000)
    cwd: str = Field(default=".", min_length=1, max_length=4096)
    timeout_seconds: int = Field(default=30, ge=1, le=120)

    @field_validator("command", "cwd")
    @classmethod
    def validate_text(cls, value: str) -> str:
        """不改写脚本语法，但无效命令不能排在其他副作用之后才被发现。"""
        if not value.strip() or "\x00" in value:
            raise ValueError("invalid command or directory")
        return value
