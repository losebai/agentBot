"""管理请求及状态响应的严格数据契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, create_model, model_validator
from pydantic_core import PydanticCustomError

from agent_bot.agent.contracts import AgentReadiness
from agent_bot.ai.call_records import CallStatistics
from agent_bot.ai.contracts import CatalogReadiness, CompletionMetadata
from agent_bot.bootstrap import BootstrapConfig
from agent_bot.config import RuntimeConfig
from agent_bot.media.contracts import MediaReadiness
from agent_bot.models import ChatMessage
from agent_bot.settings import SECRET_SETTING_NAMES
from agent_bot.static_config import STATIC_SETTING_NAMES

BOOTSTRAP_SETTING_NAMES = frozenset(field.alias for field in BootstrapConfig.model_fields.values())
STARTUP_SETTING_NAMES = STATIC_SETTING_NAMES | BOOTSTRAP_SETTING_NAMES
NATIVE_ONLY_STARTUP_SETTING_NAMES = frozenset({
    "agent_bot_napcat_mode", "agent_bot_napcat_webui_port",
})
# 旧联网轮次仅保留兼容读取，执行预算已经统一到 Agent 配置。
HIDDEN_SETTING_NAMES = frozenset({"chat_web_search_max_steps"})


class ApiModel(BaseModel):
    """Forbid unknown input fields and suppress raw values in validation messages."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class PatchBase(ApiModel):
    """Validate patch semantics while the complete runtime model validates the merged result."""

    @model_validator(mode="after")
    def validate_patch(self) -> "PatchBase":
        """拒绝空更新与秘密展示掩码，数据库密码也不能将星号占位当作新凭据。"""
        if not self.model_fields_set:
            raise ValueError("configuration update must contain a field")
        for name in self.model_fields_set & (SECRET_SETTING_NAMES | {"chat_db_password"}):
            value = getattr(self, name)
            plain = value.get_secret_value() if hasattr(value, "get_secret_value") else value
            if isinstance(plain, str) and len(plain) >= 3 and set(plain) == {"*"}:
                # 只携带已声明字段名，管理错误边界无需读取密钥值即可定位问题。
                raise PydanticCustomError(
                    "secret_mask", "a secret display mask is not a credential", {"field": name},
                )
        return self


RuntimeConfigPatch = create_model(
    "RuntimeConfigPatch",
    __base__=PatchBase,
    **{name: (field.annotation | None, None)
       for name, field in RuntimeConfig.model_fields.items() if name not in HIDDEN_SETTING_NAMES},
    **{field.alias: (field.annotation | None, None)
       for field in BootstrapConfig.model_fields.values()},
)


class SettingsUpdate(ApiModel):
    """动态字段校验数据库版本，启动字段还必须提交独立文件版本。"""

    expected_revision: int = Field(ge=1)
    startup_revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    values: RuntimeConfigPatch

    @model_validator(mode="after")
    def require_startup_revision(self) -> "SettingsUpdate":
        """启动写入不得绕过文件版本锁，其他字段仍复用一次数据库版本校验。"""
        if self.values.model_fields_set & STARTUP_SETTING_NAMES and self.startup_revision is None:
            raise ValueError("startup settings revision is required")
        return self


class SettingField(ApiModel):
    """A visible setting; secret values are absent rather than masked strings."""

    name: str
    description: str
    kind: Literal["boolean", "number", "string", "json", "secret"]
    value: JsonValue = None
    nullable: bool = False
    secret_is_set: bool = False
    restart_required: bool = False
    read_only: bool = False
    scope: Literal["static", "runtime"] = "runtime"


class SettingsSnapshot(ApiModel):
    """Expose persisted and applied state separately."""

    database_revision: int
    applied_revision: int
    startup_revision: str | None = None
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


class PluginInfo(ApiModel):
    """区分保存选择、当前进程启用和渠道运行状态。"""

    id: str
    name: str
    version: str
    description: str
    saved_enabled: bool
    enabled: bool
    status: Literal["disabled", "prepared", "running", "stopped", "failed"]
    restart_required: bool


class OverviewStatistics(CallStatistics):
    """区间统计来自数据库，运行数量来自当前进程，避免历史 running 残留误报。"""

    running_requests: int = Field(ge=0)


class ServiceStatus(ApiModel):
    """同时展示服务就绪、连接、日期区间用量与当前对话活动。"""

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
    statistics: OverviewStatistics | None = None
    statistics_error: str | None = None


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
