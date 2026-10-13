"""副作用工具跨重试保存的类型化执行结果。"""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

EffectToolName = Literal[
    "send_reply", "mention_member", "local_workflow_generate_image", "tts_generate",
    "image_start_reply", "image_completion_reply",
]


class ReplyEffect(BaseModel):
    """模型选定、由会话队列统一投递的一条最终回复。"""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)
    kind: Literal["reply"] = "reply"
    text: str = Field(min_length=1, max_length=4000)
    target_user_id: int | None = Field(default=None, gt=0)


class MentionEffect(BaseModel):
    """平台已经接收的群提醒结果。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["mention"] = "mention"
    message_id: int | str | None = None
    target_user_id: int = Field(gt=0)


class ImageEffect(BaseModel):
    """经过工具校验、待恢复到收集器的图片。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["image"] = "image"
    workflow_key: str = Field(min_length=1, max_length=128)
    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data: str = Field(min_length=1, max_length=13_981_016, repr=False)


class ImageTaskEffect(BaseModel):
    """已持久化提交的后台绘图任务，不表示图片已经生成。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["image_task"] = "image_task"
    task_id: str = Field(min_length=1, max_length=128)


class SpeechEffect(BaseModel):
    """独立于本地文件寿命保存的已生成 WAV。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    kind: Literal["speech"] = "speech"
    text: str = Field(min_length=1, max_length=4096, repr=False)
    data: str = Field(min_length=1, max_length=13_981_016, repr=False)


ToolEffect = Annotated[
    ReplyEffect | MentionEffect | ImageEffect | ImageTaskEffect | SpeechEffect,
    Field(discriminator="kind"),
]


class ToolOperation(BaseModel):
    """执行权和结果一同保存；RUNNING 不允许另一个调用重新执行。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    signature: str = Field(pattern=r"^[a-f0-9]{64}$")
    status: Literal["RUNNING", "COMPLETED"] = "RUNNING"
    result: ToolEffect | None = None

    @model_validator(mode="after")
    def validate_result(self) -> ToolOperation:
        """完成态必须有结果，执行态不得携带结果。"""

        if (self.status == "COMPLETED") != (self.result is not None):
            raise ValueError("tool operation state is invalid")
        return self


class ToolOperationConflict(RuntimeError):
    """操作正在执行、结果不明，或重试尝试改变已占用操作的参数。"""


class ToolOperationStore(Protocol):
    """副作用执行前占位、执行后保存和重试恢复的持久端口。"""

    async def begin(
        self, job_id: str, tool: EffectToolName, signature: str,
    ) -> ToolEffect | None: ...

    async def complete(
        self, job_id: str, tool: EffectToolName, signature: str, result: ToolEffect,
    ) -> None: ...

    async def completed(self, job_id: str, tool: EffectToolName) -> ToolEffect | None: ...
