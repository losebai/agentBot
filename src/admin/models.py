"""管理请求及状态响应的严格数据契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, create_model, model_validator

from agent_bot.agent.contracts import AgentReadiness
from agent_bot.ai.contracts import CatalogReadiness, CompletionMetadata
from agent_bot.config import RuntimeConfig
from agent_bot.media.contracts import MediaReadiness
from agent_bot.models import ChatMessage
from agent_bot.settings import SECRET_SETTING_NAMES


class ApiModel(BaseModel):
    """Forbid unknown input fields and suppress raw values in validation messages."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class PatchBase(ApiModel):
    """Validate patch semantics while the complete runtime model validates the merged result."""

    @model_validator(mode="after")
    def validate_patch(self) -> "PatchBase":
        """Reject empty updates and secret display masks rather than persisting them."""
        if not self.model_fields_set:
            raise ValueError("configuration update must contain a field")
        for name in self.model_fields_set & SECRET_SETTING_NAMES:
            value = getattr(self, name)
            plain = value.get_secret_value() if hasattr(value, "get_secret_value") else value
            if isinstance(plain, str) and len(plain) >= 3 and set(plain) == {"*"}:
                raise ValueError("a secret display mask is not a credential")
        return self


RuntimeConfigPatch = create_model(
    "RuntimeConfigPatch",
    __base__=PatchBase,
    **{name: (field.annotation | None, None) for name, field in RuntimeConfig.model_fields.items()},
)


class SettingsUpdate(ApiModel):
    """Update only explicitly supplied fields against one known database revision."""

    expected_revision: int = Field(ge=1)
    values: RuntimeConfigPatch


class SettingField(ApiModel):
    """A visible setting; secret values are absent rather than masked strings."""

    name: str
    description: str
    kind: Literal["boolean", "number", "string", "json", "secret"]
    value: JsonValue = None
    nullable: bool = False
    secret_is_set: bool = False
    restart_required: bool = False


class SettingsSnapshot(ApiModel):
    """Expose persisted and applied state separately."""

    database_revision: int
    applied_revision: int
    restart_required_fields: list[str]
    fields: list[SettingField]


class ApiError(ApiModel):
    """A safe error category and server-generated correlation identifier."""

    code: str
    message: str
    request_id: str


class BotInfo(ApiModel):
    """Public observations about a connected bot without credentials."""

    id: str
    adapter: str


class ServiceStatus(ApiModel):
    """数据库、媒体存储就绪与机器人连接分别展示。"""

    service: Literal["agentBot"] = "agentBot"
    ready: bool
    bot_count: int
    database_revision: int
    applied_revision: int
    restart_required_fields: list[str]
    ai_backend: Literal["python-ordinary"] = "python-ordinary"
    plain_ai_backend: Literal["python"] = "python"
    configured_model: str = ""
    agent_backend: Literal["python"] = "python"
    configured_agent: str | None = None
    python_agent: AgentReadiness | None = None
    legacy_capabilities: tuple[str, ...] = (
        "laya",
        "video",
        "soloncode",
    )
    media_backend: Literal["python"] = "python"
    job_enabled: bool = False
    auto_speak_enabled: bool = False
    python_media: MediaReadiness | None = None
    python_ai: CatalogReadiness | None = None


class ChatRequest(ApiModel):
    """A bounded ordinary-text probe with no selectable external endpoint or tools."""

    prompt: str = Field(min_length=1, max_length=2000)
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    messages: list[ChatMessage] = Field(default_factory=list, max_length=24)
    model: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_context(self) -> "ChatRequest":
        """Bound history and reject an empty current prompt."""
        if not self.prompt.strip() or sum(len(item.content) for item in self.messages) > 12000:
            raise ValueError("chat input exceeds the context budget or is empty")
        return self


class ChatResponse(ApiModel):
    """The actual upstream reply, with no synthetic success content."""

    content: str
    request_id: str
    metadata: CompletionMetadata | None = None
