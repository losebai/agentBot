"""只新增知识与记忆的有界输入、列表和模型检索结果。"""

import re
from datetime import datetime

from pydantic import ConfigDict, Field, field_validator, model_validator

from agent_bot.ai.contracts import AiModel
from agent_bot.materials.contracts import MaterialId
from agent_bot.services.tagged_memory import MemoryScope, normalize_memory_tags


class TaggedText(AiModel):
    """正文和标签共用记忆的规范化边界。"""

    model_config = ConfigDict(str_strip_whitespace=True)
    content: str = Field(min_length=1, max_length=16000)
    tags: tuple[str, ...] = Field(min_length=1, max_length=8)

    @field_validator("tags")
    @classmethod
    def normalized_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """新知识与已有记忆使用相同主题命名。"""
        return normalize_memory_tags(value)

    @field_validator("content", check_fields=False)
    @classmethod
    def valid_text(cls, value: str) -> str:
        """正文允许换行，但拒绝空内容和数据库 NUL。"""
        if not value.strip() or "\0" in value:
            raise ValueError("invalid text")
        return value


class KnowledgeCreate(TaggedText):
    """管理员新增完整知识条目，新增后标题和标签不可修改。"""

    title: str = Field(min_length=1, max_length=200)
    _title = field_validator("title")(TaggedText.valid_text.__func__)


class KnowledgeView(KnowledgeCreate):
    """完整知识详情和管理列表的公开字段。"""

    id: int
    created_at: datetime


class MemoryCreate(TaggedText):
    """已有匿名作用域与新 QQ 身份互斥，不能推测原始身份。"""

    scope: MemoryScope
    key: str = Field(min_length=1, max_length=80)
    content: str = Field(min_length=1, max_length=1000)
    scope_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    bot_id: str | None = Field(default=None, pattern=r"^[1-9][0-9]{0,19}$")
    target: str | None = Field(default=None, min_length=1, max_length=67)
    _key = field_validator("key")(TaggedText.valid_text.__func__)

    @model_validator(mode="after")
    def valid_scope(self) -> "MemoryCreate":
        """仅允许管理员明确指定一种来源，session 必须使用真实外部会话标识。"""
        if self.scope_id:
            if self.bot_id is not None or self.target is not None:
                raise ValueError("conflicting memory identity")
        elif not self.bot_id:
            raise ValueError("bot identity required")
        elif self.scope == "global":
            if self.target is not None:
                raise ValueError("global memory has no target")
        else:
            pattern = r"qq_[a-f0-9]{32}" if self.scope == "session" else r"[1-9][0-9]{0,19}"
            if not self.target or not re.fullmatch(pattern, self.target):
                raise ValueError("invalid memory target")
        return self


class MemoryView(AiModel):
    """管理员可查看匿名归属，模型仍不能选择这些管理字段。"""

    id: int
    scope_id: str
    scope: MemoryScope
    key: str
    content: str
    tags: tuple[str, ...]
    created_at: datetime
    material_ids: tuple[MaterialId, ...] = ()


class TagCount(AiModel):
    """仅统计当前有效记录的标签条目数。"""

    tag: str
    count: int


class ScopeView(AiModel):
    """不可逆作用域及有效记忆数，不附加猜测的 QQ 名称。"""

    scope_id: str
    scope: MemoryScope
    count: int


class KnowledgeSearchArguments(AiModel):
    """模型只能按文本与主题读取资料，不提供写入能力。"""

    model_config = ConfigDict(str_strip_whitespace=True)
    query: str = Field(default="", max_length=200)
    tags: tuple[str, ...] = Field(default=(), max_length=8)
    _tags = field_validator("tags")(TaggedText.normalized_tags.__func__)

    @model_validator(mode="after")
    def has_query(self) -> "KnowledgeSearchArguments":
        """禁止空条件遍历整个知识库。"""
        if (not self.query and not self.tags) or "\0" in self.query:
            raise ValueError("query or tags required")
        return self


class KnowledgeMatch(AiModel):
    """带真实来源和截断状态的模型可见正文。"""

    id: int
    title: str
    content: str
    tags: tuple[str, ...]
    truncated: bool
