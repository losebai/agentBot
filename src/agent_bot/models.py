"""聊天网关的类型化数据模型。"""

import unicodedata
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ChatToolName = Literal[
    "soloncode",
    "job",
    "memory",
    "naraka_query",
    "naraka_search",
    "mention_member",
    "image_generate",
    "local_workflow_list",
    "local_workflow_generate_image", "local_workflow_generate_video",
    "local_workflow_tasks",
    "local_workflow_cancel",
    "tts_generate",
    "bilibili_login_status", "bilibili_get_comments",
    "bilibili_reply_comment", "bilibili_publish_video",
]
AgentToolName = Literal[
    "soloncode",
    "job",
    "memory",
    "material_search",
    "material_get",
    "web_search",
    "read_url",
    "naraka_query",
    "naraka_search",
    "mention_member",
    "image_generate",
    "local_workflow_list",
    "local_workflow_generate_image", "local_workflow_generate_video",
    "local_workflow_tasks",
    "local_workflow_cancel",
    "tts_generate",
    "bilibili_login_status", "bilibili_get_comments",
    "bilibili_reply_comment", "bilibili_publish_video",
]
AgentEventType = Literal["reason", "text", "action", "result", "done", "error", "images"]
MediaOutputType = Literal["IMAGE", "AUDIO"]

_QQ_CONTEXT_ID_PATTERN = r"^[1-9][0-9]{4,19}$"
_MAX_CONTEXT_NAME_UTF16_UNITS = 128


class AiJobResult(BaseModel):
    """AI 持久任务的成功结果及不可由模型指定的原始投递目标。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    bot_id: str = Field(alias="botId", pattern=_QQ_CONTEXT_ID_PATTERN)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=255)
    sender_id: str = Field(alias="senderId", pattern=_QQ_CONTEXT_ID_PATTERN)
    scene: Literal["private", "group"]
    group_id: str | None = Field(default=None, alias="groupId", pattern=_QQ_CONTEXT_ID_PATTERN)
    prompt: str = Field(min_length=1, max_length=8000)
    status: Literal["SUCCEEDED"]
    result: str = Field(min_length=1, max_length=64000)
    run_at: str = Field(alias="runAt", max_length=40)

    @model_validator(mode="after")
    def validate_target(self) -> "AiJobResult":
        """禁止群聊结果缺失群号或私聊混入群目标。"""
        if (self.scene == "group") != (self.group_id is not None):
            raise ValueError("AI job result target is invalid")
        if not self.result.strip():
            raise ValueError("AI job result is empty")
        return self


class AiJobResultsResponse(BaseModel):
    """一次拉取最多十条成功结果。"""

    code: int
    data: tuple[AiJobResult, ...] = Field(default=(), max_length=10)


class ChatMessage(BaseModel):
    """本地 AI 服务使用的单条聊天消息。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    role: Literal["system", "user", "assistant"]
    history_request_id: str | None = Field(
        default=None, alias="historyRequestId", min_length=1, max_length=128,
        pattern=r"^[^\s\x00-\x1f\x7f]+$", exclude_if=lambda value: value is None,
    )
    content: str = Field(min_length=1)


class ChatContext(BaseModel):
    """当前 QQ 消息的类型化来源信息。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    platform: Literal["qq"] = "qq"
    bot_id: str | None = Field(
        default=None, alias="botId", pattern=_QQ_CONTEXT_ID_PATTERN,
        exclude_if=lambda value: value is None,
    )
    scene: Literal["private", "group"]
    group_id: str | None = Field(
        default=None,
        alias="groupId",
        pattern=_QQ_CONTEXT_ID_PATTERN,
        repr=False,
    )
    group_name: str | None = Field(default=None, alias="groupName", repr=False)
    sender_id: str = Field(
        alias="senderId",
        pattern=_QQ_CONTEXT_ID_PATTERN,
        repr=False,
    )
    sender_name: str | None = Field(default=None, alias="senderName", repr=False)

    @field_validator("group_name", "sender_name")
    @classmethod
    def validate_display_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(
            unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"}
            for character in value
        ):
            raise ValueError("chat context name contains unsupported characters")
        normalized = " ".join(unicodedata.normalize("NFKC", value).split())
        utf16_units = sum(2 if ord(character) > 0xFFFF else 1 for character in normalized)
        if not normalized or utf16_units > _MAX_CONTEXT_NAME_UTF16_UNITS:
            raise ValueError("chat context name is empty or too long")
        return normalized

    @model_validator(mode="after")
    def validate_scene_fields(self) -> "ChatContext":
        if self.scene == "group" and self.group_id is None:
            raise ValueError("group chat context requires groupId")
        if self.scene == "private" and (
            self.group_id is not None or self.group_name is not None
        ):
            raise ValueError("private chat context cannot contain group fields")
        return self


class LlmRequestOptions(BaseModel):
    """可选的模型采样参数；空值表示继续使用模型提供方默认值。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    max_tokens: int | None = Field(default=None, alias="maxTokens", ge=1, le=128_000)
    temperature: float | None = Field(
        default=None,
        ge=0,
        le=2,
        allow_inf_nan=False,
    )
    top_p: float | None = Field(
        default=None,
        alias="topP",
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    top_k: int | None = Field(default=None, alias="topK", ge=1, le=100)
    min_p: float | None = Field(
        default=None,
        alias="minP",
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    presence_penalty: float | None = Field(
        default=None,
        alias="presencePenalty",
        ge=-2,
        le=2,
        allow_inf_nan=False,
    )
    frequency_penalty: float | None = Field(
        default=None,
        alias="frequencyPenalty",
        ge=-2,
        le=2,
        allow_inf_nan=False,
    )
    repetition_penalty: float | None = Field(
        default=None,
        alias="repetitionPenalty",
        ge=1,
        le=2,
        allow_inf_nan=False,
    )
    stop: tuple[str, ...] | None = None
    enable_thinking: bool | None = Field(default=None, alias="enableThinking")


class AutoSpeakDecisionRequest(BaseModel):
    """本地自动发言决策使用的有界请求。"""

    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        extra="forbid",
        strict=True,
    )

    state: str = Field(min_length=1, max_length=8000)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=256)
    request_id: str = Field(alias="requestsId", min_length=1, max_length=256)
    request_timeout_millis: int = Field(
        alias="requestTimeoutMillis",
        ge=1,
        le=300_000,
    )

    @field_validator("state", "session_id")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        """拒绝只有空白的状态或会话标识，同时保持提交文本不变。"""
        if not value.strip():
            raise ValueError("auto-speak decision text is empty")
        return value


class AutoSpeakDecisionResponse(BaseModel):
    """已登记 MCP 决策工具返回的固定 choice 结果。"""

    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        extra="forbid",
        strict=True,
    )

    should_speak: bool = Field(alias="shouldSpeak")
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    model: Literal["multilingual"]


class ChatMediaRequest(BaseModel):
    """本地媒体服务使用的单次生成请求。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")
    request_timeout_millis: int | None = Field(
        default=None, alias="requestTimeoutMillis", ge=1, le=300_000
    )

    request_id: str = Field(alias="requestId", min_length=1, max_length=128)
    session_id: str = Field(alias="sessionId", min_length=1, max_length=255)
    user: str = Field(min_length=1, max_length=128)
    model: str = Field(min_length=1, max_length=255)
    output_type: MediaOutputType = Field(alias="outputType")
    prompt: str | None = Field(default=None, max_length=4096)
    messages: tuple[ChatMessage, ...] = Field(default_factory=tuple, max_length=64)
    timeout_seconds: int = Field(alias="timeoutSeconds", ge=1, le=120)
    max_bytes: int = Field(alias="maxBytes", ge=1024, le=10_485_760)
    image_format: Literal["png", "jpeg", "webp"] | None = Field(
        default=None,
        alias="imageFormat",
    )
    voice: str | None = Field(default=None, min_length=1, max_length=255)
    audio_format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] | None = Field(
        default=None,
        alias="audioFormat",
    )

    @model_validator(mode="after")
    def validate_output_fields(self) -> "ChatMediaRequest":
        if self.output_type == "IMAGE":
            if not self.prompt or not self.prompt.strip() or self.image_format is None:
                raise ValueError("image generation requires prompt and imageFormat")
            if self.messages or self.voice is not None or self.audio_format is not None:
                raise ValueError("image generation contains audio fields")
        elif (
            self.prompt is not None
            or not self.messages
            or self.image_format is not None
            or self.voice is None
            or self.audio_format is None
        ):
            raise ValueError("audio generation fields are invalid")
        return self


class ChatMediaItem(BaseModel):
    """经过校验的唯一 Base64 媒体项。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    type: MediaOutputType
    mime_type: str = Field(alias="mimeType", min_length=1, max_length=64)
    data: str = Field(min_length=1, repr=False)


class ChatMediaResponse(BaseModel):
    """本地媒体响应数据。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str | None = Field(default=None, max_length=65_536)
    items: tuple[ChatMediaItem, ...] = Field(min_length=1, max_length=1)
