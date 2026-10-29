"""表情检索与投递的模型参数，不接受文件路径、外部URL或伪造用户身份。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_bot.plugins.stickers.contracts import normalize_tags

STICKER_TOOLS = frozenset({"sticker_search", "sticker_send"})


class StickerSearchArguments(BaseModel):
    """上下文表达本轮语气与意图，标签是管理员共享库的显式过滤条件。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    context: str = Field(min_length=1, max_length=4000)
    tags: tuple[str, ...] = Field(default=(), max_length=16)
    limit: int = Field(default=3, ge=1, le=5)

    @field_validator("tags", mode="before")
    @classmethod
    def valid_tags(cls, value: object) -> tuple[str, ...]:
        """检索与上传标签共用规范，不解释通配符或模型指令。"""
        return normalize_tags(value)


class StickerSendArguments(BaseModel):
    """只接受本轮已经检索过的图库ID，真实发送目标由宿主绑定。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
