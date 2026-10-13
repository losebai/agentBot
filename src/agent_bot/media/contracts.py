"""严格区分模型图片字节、文件归属和可公开的媒体元数据。"""

import base64
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field, field_validator, model_validator

from agent_bot.ai.contracts import AiModel
from agent_bot.media.config import MAX_IMAGE_BYTES, MAX_MODEL_IMAGE_BYTES
from agent_bot.models import ChatMediaResponse

ImageType = Literal["image/png", "image/jpeg", "image/webp"]
MediaType = Literal[
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "audio/mpeg",
    "audio/ogg",
    "audio/aac",
    "audio/flac",
    "audio/wav",
    "audio/pcm",
]
RunStatus = Literal["running", "completed", "failed", "cancelled"]
AssetStatus = Literal["writing", "ready", "expired", "failed"]


class MediaOwner(AiModel):
    """可信入口创建的会话归属，不包含可由模型伪造的 QQ 身份。"""

    entry: Literal["admin", "qq"]
    session_id: str = Field(min_length=1, max_length=256)


class MediaScope(AiModel):
    """一次执行的文件作用域，绑定入口及稳定执行标识。"""

    owner: MediaOwner
    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    execution_kind: Literal["agent", "media"]


class ModelImage(AiModel):
    """有界模型图像，二进制仅在协议适配时显式编码。"""

    media_type: ImageType
    data: bytes = Field(min_length=1, max_length=MAX_MODEL_IMAGE_BYTES, repr=False, exclude=True)
    source_size_bytes: int = Field(gt=0, le=MAX_IMAGE_BYTES)

    def data_url(self) -> str:
        """仅供模型协议显式调用，不参与日志或默认序列化。"""
        return f"data:{self.media_type};base64,{base64.b64encode(self.data).decode('ascii')}"


class ValidatedImage(AiModel):
    """已校验原图用于工作流，去元数据的副本用于模型理解。"""

    media_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"]
    data: bytes = Field(min_length=1, max_length=MAX_IMAGE_BYTES, repr=False, exclude=True)
    model_image: ModelImage = Field(repr=False, exclude=True)


class MediaAssetView(AiModel):
    """文件公开元数据不包含磁盘位置、身份和内容字节。"""

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    purpose: Literal["input", "output"]
    mime_type: MediaType
    size_bytes: int = Field(gt=0, le=MAX_IMAGE_BYTES)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: AssetStatus
    created_at: datetime
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def utc_time(cls, value):
        """数据库统一保存无时区的 UTC 时间。"""
        return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value

    @model_validator(mode="after")
    def valid_lifetime(self):
        """文件有效期必须晚于创建时间，不能永久占用配额。"""
        if self.expires_at <= self.created_at:
            raise ValueError("invalid media lifetime")
        return self


class MediaAssetRecord(MediaAssetView):
    """仅存储边界使用的媒体条目，执行字段必须成对存在。"""

    owner: MediaOwner
    storage_key: str = Field(pattern=r"^[0-9a-f]{32}$", repr=False)
    execution_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    execution_kind: Literal["agent", "media"] | None = None

    @model_validator(mode="after")
    def valid_execution(self):
        """半个执行标识或与文件 ID 不同的存储键均不可持久化。"""
        if (self.execution_id is None) != (self.execution_kind is None):
            raise ValueError("incomplete media execution")
        if self.storage_key != self.id:
            raise ValueError("invalid storage key")
        return self

    def public(self) -> MediaAssetView:
        """显式选取公开列，防止内部字段随继承关系泄漏。"""
        return MediaAssetView.model_validate(
            self.model_dump(include=set(MediaAssetView.model_fields))
        )


class MediaRunSummary(AiModel):
    """媒体执行摘要与 Agent 运行记录分开，避免伪造工具轮次。"""

    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    entry: Literal["admin", "qq"]
    session_id: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    model: str = Field(min_length=1, max_length=255)
    backend: Literal["python-media"] = "python-media"
    output_type: Literal["IMAGE", "AUDIO"]
    status: RunStatus = "running"
    duration_ms: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=100)
    created_at: datetime


class MediaRunStart(MediaRunSummary):
    """开始执行时记录有限输入，生成期间不记录原始媒体。"""

    prompt: str = Field(max_length=32000)
    content: str = Field(default="", max_length=65536)


class MediaRunFinish(AiModel):
    """只允许把进行中的记录收束为确定的终态。"""

    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: Literal["completed", "failed", "cancelled"]
    duration_ms: int = Field(ge=0)
    content: str = Field(default="", max_length=65536)
    error_code: str | None = Field(default=None, max_length=100)


class MediaRunDetail(MediaRunStart):
    """执行详情包含附件元数据，过期文件不自动重新生成。"""

    attachments: tuple[MediaAssetView, ...] = ()


class MediaGenerationResult(AiModel):
    """QQ 私有响应与管理接口公开附件共用一个生成结果。"""

    execution_id: str
    request_id: str
    backend: Literal["python-media"] = "python-media"
    model: str
    text: str | None = None
    attachments: tuple[MediaAssetView, ...] = ()
    duration_ms: int = Field(ge=0)
    record_saved: bool
    response: ChatMediaResponse = Field(repr=False, exclude=True)


class MediaReadiness(AiModel):
    """只描述本地媒体资源，不为健康检查调用模型。"""

    initialized: bool = False
    storage_available: bool = False
    code: str | None = None
