"""任务命令和可信所有者契约，模型只能提出任务正文与时间。"""

import hashlib
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

QQ_ID = r"^[1-9][0-9]{4,19}$"
JOB_ID = r"^[a-f0-9]{32}$"


class JobModel(BaseModel):
    """公共模型拒绝额外字段，保留原 camelCase 投递协议。"""

    model_config = ConfigDict(
        extra="forbid", frozen=True, populate_by_name=True, hide_input_in_errors=True
    )


class JobOwner(JobModel):
    """由 QQ 认证会话提供的三元所有权及固定投递目标。"""

    bot_id: str = Field(pattern=QQ_ID, alias="botId")
    session_id: str = Field(min_length=1, max_length=256, alias="sessionId")
    sender_id: str = Field(pattern=QQ_ID, alias="senderId")
    scene: Literal["group", "private"]
    group_id: str | None = Field(default=None, pattern=QQ_ID, alias="groupId")

    @model_validator(mode="after")
    def target(self):
        """群聊必须有群号，私聊不能携带群目标。"""
        if not self.session_id.strip() or (self.scene == "group") != (self.group_id is not None):
            raise ValueError("invalid job owner")
        return self

    @property
    def owner_key(self):
        """稳定摘要只用于所有者锁，原身份仍随任务保存。"""
        payload = json.dumps([self.bot_id, self.session_id, self.sender_id], separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


class JobScope(JobOwner):
    """宿主快照提供请求幂等标识、模型别名和系统提示，不持久化凭据。"""

    request_id: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=200)
    system_prompt: str = Field(default="", max_length=32000, repr=False)

    @field_validator("request_id", "model")
    @classmethod
    def nonblank(cls, value):
        """空白标识不能作为幂等键或模型引用。"""
        if not value.strip():
            raise ValueError("empty job identity")
        return value

    @property
    def deduplication_key(self):
        """同一所有者的一次请求只创建一个任务，正文变化不绕过幂等。"""
        return hashlib.sha256(f"{self.owner_key}:{self.request_id}".encode()).hexdigest()


class JobCommand(JobModel):
    """四种工具动作，日期在创建事务前结合服务器时钟校验。"""

    action: Literal["create", "list", "get", "cancel"]
    prompt: str | None = Field(default=None, min_length=1, max_length=8000)
    run_at: str | None = Field(default=None, max_length=40, alias="runAt")
    delay_seconds: int | None = Field(
        default=None, strict=True, ge=1, le=31536000, alias="delaySeconds"
    )
    job_id: str | None = Field(default=None, pattern=JOB_ID, alias="jobId")

    @model_validator(mode="after")
    def action_fields(self):
        """在任何副作用之前拒绝无效或混杂参数。"""
        if self.action == "create":
            if not self.prompt or not self.prompt.strip() or self.job_id is not None:
                raise ValueError("invalid creation")
            if (self.run_at is None) == (self.delay_seconds is None):
                raise ValueError("choose exactly one schedule")
        elif self.prompt is not None or self.run_at is not None or self.delay_seconds is not None:
            raise ValueError("unexpected creation parameters")
        elif (self.action in {"get", "cancel"}) != (self.job_id is not None):
            raise ValueError("invalid job identifier")
        return self


class JobView(JobOwner):
    """公开状态和有限正文，不暴露系统提示或数据库幂等键。"""

    id: str = Field(pattern=JOB_ID)
    prompt: str = Field(max_length=8000)
    status: Literal["PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"]
    result: str | None = Field(default=None, max_length=64000)
    error_code: str | None = Field(default=None, alias="errorCode")
    run_at: datetime = Field(alias="runAt")

    @field_validator("run_at", mode="after")
    @classmethod
    def utc_time(cls, value):
        """数据库无时区的 DATETIME 按约定恢复为 UTC。"""
        return (
            value.replace(tzinfo=timezone.utc)
            if value.tzinfo is None
            else value.astimezone(timezone.utc)
        )


class JobArguments(JobModel):
    """沿用 job 工具的 command 包装，拒绝额外宿主身份参数。"""

    command: JobCommand


class JobRecord(JobView):
    """仅执行器读取的内部任务快照。"""

    model: str
    system_prompt: str = Field(repr=False, exclude=True)
