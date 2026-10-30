"""记忆生命周期的有界管理契约，不允许模型指定来源身份。"""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MemoryKind = Literal["relationship", "preference", "agreement", "task", "fact", "correction"]
MemoryStatus = Literal["active", "pending", "disputed", "expired", "merged", "deleted"]


class MemoryStatistics(BaseModel):
    """当前有效事实总量与区间内已完成召回的命中率，不推算旧回执分母。"""

    memory_count: int = Field(ge=0)
    recall_count: int = Field(ge=0)
    recall_hit_count: int = Field(ge=0)
    recall_rate: float | None = Field(default=None, ge=0, le=1)


class QuotedMemorySource(BaseModel):
    """服务端从真实引用绑定的有界资料，模型不能指定作者或平台消息身份。"""

    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=20_000, repr=False)
    event_id: str = Field(min_length=1, max_length=128)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    time: datetime


class MemoryUpdate(BaseModel):
    """省略字段保持原值；只有有效期允许用 null 显式清空。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    content: str | None = Field(default=None, min_length=1, max_length=1000)
    tags: tuple[str, ...] | None = Field(default=None, min_length=1, max_length=8)
    topic: str | None = Field(default=None, min_length=1, max_length=80)
    kind: MemoryKind | None = None
    valid_from: datetime | None = None
    valid_until: datetime | None = None
    task_state: Literal["open", "completed", "cancelled"] | None = None
    reason: str = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_patch(self):
        """避免 null 误清正文与标签，并要求时间携带真实时区。"""
        for name in self.model_fields_set - {"valid_from", "valid_until"}:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        for name in ("valid_from", "valid_until"):
            value = getattr(self, name)
            if value is not None:
                if value.utcoffset() is None:
                    raise ValueError("memory time requires timezone")
                setattr(self, name, value.astimezone(timezone.utc))
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("invalid memory validity interval")
        if self.content and (not self.content.strip() or "\0" in self.content):
            raise ValueError("invalid memory content")
        return self


class MemoryTarget(BaseModel):
    """批量变更必须逐项绑定当前版本。"""

    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=1)
    expected_revision: int = Field(ge=1)


class MemoryMerge(BaseModel):
    """合并只能作用于显式列出的同主体事实，不按标签盲目覆盖。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target_id: int = Field(ge=1)
    items: tuple[MemoryTarget, ...] = Field(min_length=2, max_length=8)
    content: str | None = Field(default=None, min_length=1, max_length=1000)
    reason: str = Field(min_length=1, max_length=300)
    digest: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class MemoryCleanupPreview(BaseModel):
    """主题只用于生成预览，提交必须包含确定的目标集合。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    scope_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    topic: str = Field(min_length=1, max_length=80)
    action: Literal["expire", "forget"]


class MemoryResolve(BaseModel):
    """采用某个现有冲突结论，必须提交整个集合的版本。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target_id: int = Field(ge=1)
    items: tuple[MemoryTarget, ...] = Field(min_length=2, max_length=100)
    reason: str = Field(min_length=1, max_length=300)


class MemoryCleanup(BaseModel):
    """预览后出现的同主题新记录不在清理范围内。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    items: tuple[MemoryTarget, ...] = Field(min_length=1, max_length=100)
    action: Literal["expire", "forget"]
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=1, max_length=300)


class MemoryEvidence(BaseModel):
    """来源摘要可解释依据，正文片段不是可执行指令。"""

    id: int
    source_kind: str
    source_ref: str | None = None
    source_time: datetime | None = None
    excerpt: str = ""
