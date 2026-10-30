"""表情检索与投递的模型参数，不接受文件路径、外部URL或伪造用户身份。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent_bot.plugins.stickers.contracts import normalize_tags

STICKER_TOOLS = frozenset({"sticker_search", "sticker_send"})


class StickerSearchArguments(BaseModel):
    """聊天模型只描述语境，不猜测管理员标签而把真实候选过滤为空。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    context: str = Field(min_length=1, max_length=4000,
                         description="本轮对话含义、希望表达的情绪和语气；不是标签筛选")
    limit: int = Field(default=3, ge=1, le=5)


class StickerLibrarySearchArguments(StickerSearchArguments):
    """管理页保留人工指定标签的严格交集筛选，与聊天工具参数分离。"""

    tags: tuple[str, ...] = Field(default=(), max_length=16)

    @field_validator("tags", mode="before")
    @classmethod
    def valid_tags(cls, value: object) -> tuple[str, ...]:
        """检索与上传标签共用规范，不解释通配符或模型指令。"""
        return normalize_tags(value)


class StickerSendArguments(BaseModel):
    """只接受本轮已经检索过的图库ID，真实发送目标由宿主绑定。"""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
