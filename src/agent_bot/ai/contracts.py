"""配置、普通文本、媒体能力及调用记录的有界数据契约。"""

from datetime import datetime
from typing import Generic, Literal, TypeVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from agent_bot.credential_values import CredentialSnapshot, CredentialValue
from agent_bot.models import ChatMessage, LlmRequestOptions

ProviderProtocol = Literal["openai-responses", "openai-chat", "ollama"]
CallStatus = Literal["completed", "failed", "cancelled"]
T = TypeVar("T")


class AiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, frozen=True)


class ModelMediaCapabilities(AiModel):
    """管理员明确声明的媒体能力，不通过模型名称推断。"""

    vision_enabled: bool = False
    image_api_profile: Literal["none", "gpt-image", "legacy-b64"] = "none"
    audio_chat_enabled: bool = False


class ProviderFields(AiModel):
    """供应商可公开的连接信息，不包含写入凭据。"""

    name: str = Field(min_length=1, max_length=100)
    protocol: ProviderProtocol
    base_url: str = Field(min_length=1, max_length=2048)
    enabled: bool = False
    description: str = Field(default="", max_length=1000)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("invalid name")
        return value

    @field_validator("base_url")
    @classmethod
    def check_url(cls, value: str) -> str:
        if any(ord(char) <= 32 or ord(char) == 127 for char in value):
            raise ValueError("invalid endpoint")
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("invalid endpoint")
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError("invalid endpoint port")
        path = parts.path.rstrip("/")
        if path.endswith(("/responses", "/chat/completions", "/api/chat")):
            raise ValueError("use API root rather than complete endpoint")
        return value.rstrip("/")


class ProviderCreate(ProviderFields):
    """创建供应商时直接接收密钥，不使用环境变量引用。"""

    credential: CredentialValue | None = Field(default=None, repr=False)


class ProviderView(ProviderFields, CredentialSnapshot):
    """公开字段与不序列化的内部凭据快照分离。"""

    id: int
    revision: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProviderUpdate(AiModel):
    """版本化修改供应商；省略凭据保留，显式 null 清除。"""

    expected_revision: int = Field(ge=1)
    name: str | None = None
    protocol: ProviderProtocol | None = None
    base_url: str | None = None
    credential: CredentialValue | None = Field(default=None, repr=False)
    enabled: bool | None = None
    description: str | None = None


class ModelCreate(AiModel):
    """模型别名与显式媒体能力，省略能力时兼容旧目录。"""

    alias: str = Field(min_length=1, max_length=200)
    upstream_model: str = Field(min_length=1, max_length=200)
    provider_id: int = Field(ge=1)
    enabled: bool = False
    media: ModelMediaCapabilities = Field(default_factory=ModelMediaCapabilities)

    @field_validator("alias", "upstream_model")
    @classmethod
    def check_text(cls, value: str) -> str:
        """禁止空名称和控制字符进入目录。"""
        if not value.strip() or any(ord(char) < 32 for char in value):
            raise ValueError("invalid model name")
        return value


class ModelView(ModelCreate):
    """模型目录快照及媒体表安装状态。"""

    media_initialized: bool = False
    id: int
    revision: int
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ModelUpdate(AiModel):
    """能力省略时保留，传入对象时完整替换。"""

    expected_revision: int = Field(ge=1)
    alias: str | None = None
    upstream_model: str | None = None
    provider_id: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    media: ModelMediaCapabilities | None = None

    @field_validator("media")
    @classmethod
    def reject_null_media(cls, value):
        """显式空值不可误解为清空或保留配置。"""
        if value is None:
            raise ValueError("media cannot be null")
        return value


class VersionDelete(AiModel):
    expected_revision: int = Field(ge=1)


class UpstreamModels(AiModel):
    """发现结果绑定供应商版本，禁止在切换配置后误用旧列表。"""

    provider_id: int
    revision: int
    models: tuple[str, ...]


class ModelImportItem(AiModel):
    """管理员选择的准确上游 ID 与独立本地别名。"""

    alias: str
    upstream_model: str

    @model_validator(mode="after")
    def validate_names(self):
        """复用手动登记的名称约束，不根据名称推断能力。"""
        ModelCreate(alias=self.alias, upstream_model=self.upstream_model, provider_id=1)
        return self


class ModelImportRequest(AiModel):
    """一个供应商的一批导入，在版本检查后原子写入。"""

    expected_revision: int = Field(ge=1)
    models: tuple[ModelImportItem, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_names(self):
        """批内重复直接拒绝，避免不明确的别名覆盖。"""
        for field in ("alias", "upstream_model"):
            names = [getattr(item, field) for item in self.models]
            if len(set(names)) != len(names):
                raise ValueError("duplicate import model")
        return self


class ModelImportResult(AiModel):
    """明确区分新登记和已经登记的上游模型。"""

    provider_id: int
    revision: int
    imported: tuple[ModelView, ...]
    skipped: tuple[str, ...]


class Page(AiModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class ResolvedModel(AiModel):
    model: ModelView
    provider: ProviderView


class CatalogReadiness(AiModel):
    initialized: bool
    model_available: bool = False
    provider_available: bool = False
    credential_available: bool = False
    code: str | None = None


class TextRequest(AiModel):
    model: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    prompt: str = Field(min_length=1, max_length=32000)
    messages: tuple[ChatMessage, ...] = Field(default=(), max_length=100)
    options: LlmRequestOptions = Field(default_factory=LlmRequestOptions)
    timeout_seconds: float = Field(default=300, gt=0, le=300, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_messages(self):
        messages = self.messages or (ChatMessage(role="user", content=self.prompt),)
        if not self.prompt.strip() or not self.model.strip():
            raise ValueError("empty text")
        if (
            messages[-1].role != "user"
            or not messages[-1].content.strip()
            or any(len(item.content) > 32000 for item in messages)
            or sum(len(item.content) for item in messages) > 120000
        ):
            raise ValueError("invalid text conversation")
        object.__setattr__(self, "messages", messages)
        return self


class ProviderCall(AiModel):
    request: TextRequest
    resolved: ResolvedModel
    credential: SecretStr | None = Field(default=None, repr=False, exclude=True)


class TokenUsage(AiModel):
    """模型上报的输入、输出和缓存输入用量；未上报字段保留未知值。"""

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    cache_prompt_tokens: int | None = Field(default=None, ge=0)

    def add(self, other: "TokenUsage") -> "TokenUsage":
        """累计 Agent 轮次；普通用量保持未知语义，缓存只累计具有有效输入分母的样本。"""
        values = {
            field: getattr(self, field) + getattr(other, field)
            if getattr(self, field) is not None and getattr(other, field) is not None else None
            for field in ("prompt_tokens", "completion_tokens", "total_tokens")
        }
        samples = [
            (usage.cached_tokens, usage.cache_prompt_tokens
             if usage.cache_prompt_tokens is not None else usage.prompt_tokens)
            for usage in (self, other)
        ]
        samples = [(cached, prompt) for cached, prompt in samples
                   if cached is not None and prompt is not None]
        return TokenUsage(
            **values,
            cached_tokens=sum(cached for cached, _ in samples) if samples else None,
            cache_prompt_tokens=sum(prompt for _, prompt in samples) if samples else None,
        )


class ProviderResult(AiModel):
    content: str
    model: str
    finish_reason: str = "stop"
    usage: TokenUsage = Field(default_factory=TokenUsage)


class ProviderDelta(AiModel):
    kind: Literal["content", "done"]
    content: str = ""
    model: str | None = None
    finish_reason: str = "stop"
    usage: TokenUsage = Field(default_factory=TokenUsage)


class CompletionMetadata(AiModel):
    execution_id: str
    request_id: str
    model: str
    finish_reason: str
    usage: TokenUsage
    duration_ms: int
    record_saved: bool


class AiCompletion(CompletionMetadata):
    content: str

    def metadata(self) -> CompletionMetadata:
        return CompletionMetadata.model_validate(self.model_dump(exclude={"content"}))


class AiEvent(AiModel):
    kind: Literal["start", "delta", "done", "error"]
    request_id: str
    delta: str | None = None
    metadata: CompletionMetadata | None = None
    code: str | None = None
    message: str | None = None


class SessionSummary(AiModel):
    execution_id: str
    session_id: str
    request_id: str
    configured_model: str
    model: str
    status: CallStatus
    duration_ms: int
    usage: TokenUsage
    created_at: datetime
    error_code: str | None = None


class SessionDetail(SessionSummary):
    prompt: str
    content: str


class SessionWrite(SessionDetail):
    """写入统一记录的请求快照，旧会话表仍使用原列集合。"""

    request_context: dict | None = None
