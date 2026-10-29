"""表情包公开元数据与稀疏更新契约，文件位置仅保留在存储层。"""

import unicodedata
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from agent_bot.ai.contracts import AiModel


def normalize_tags(tags: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    """修剪及去重管理员标签，保留输入顺序而不推断模型标签。"""
    if not isinstance(tags, (tuple, list)) or len(tags) > 16:
        raise ValueError('标签数量不能超过16')
    result = []
    for tag in tags:
        if (not isinstance(tag, str) or not 1 <= len(tag.strip()) <= 32
                or any(unicodedata.category(char).startswith('C') for char in tag)):
            raise ValueError('标签长度必须为1至32')
        value = tag.strip()
        if value not in result:
            result.append(value)
    return tuple(result)


class StickerView(AiModel):
    """管理和检索共用的安全视图，不泄漏文件键或目录。"""

    id: str = Field(pattern=r'^[0-9a-f]{32}$')
    name: str = Field(min_length=1, max_length=255)
    tags: tuple[str, ...] = ()
    description: str = Field(default='', max_length=1000)
    emotion: str = Field(default='', max_length=100)
    usage_hint: str = Field(default='', max_length=1000)
    enabled: bool = True
    revision: int = Field(ge=1)
    mime_type: Literal['image/png', 'image/jpeg', 'image/webp', 'image/gif']
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    animated: bool
    analysis_status: Literal['pending', 'ready', 'failed'] = 'pending'
    analysis_error: str | None = None
    created_at: datetime
    updated_at: datetime

    @field_validator('tags', mode='before')
    @classmethod
    def validate_tags(cls, value: Any) -> tuple[str, ...]:
        """所有公开标签采用相同规范。"""
        return normalize_tags(value)


class StickerPatch(AiModel):
    """只更新显式提交的字段，防止陈旧表单覆盖未编辑内容。"""

    expected_revision: int = Field(ge=1, strict=True)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    tags: tuple[str, ...] | None = None
    description: str | None = Field(default=None, max_length=1000)
    emotion: str | None = Field(default=None, max_length=100)
    usage_hint: str | None = Field(default=None, max_length=1000)
    enabled: bool | None = Field(default=None, strict=True)

    @field_validator('tags', mode='before')
    @classmethod
    def validate_tags(cls, value: Any) -> tuple[str, ...]:
        """显式空数组清除标签；省略字段维持原标签。"""
        return normalize_tags(value)

    @model_validator(mode='after')
    def reject_null(self) -> 'StickerPatch':
        """说明可以清空字符串，但显式 null 不表示保留旧值。"""
        if any(getattr(self, name) is None for name in self.model_fields_set):
            raise ValueError('更新字段不能为null')
        if self.name is not None and not self.name.strip():
            raise ValueError('名称不能为空白')
        return self
