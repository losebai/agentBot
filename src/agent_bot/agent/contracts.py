"""智能体目录、入口授权、模型轮次和执行结果的类型契约。"""

import json
import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, create_model, field_validator, model_validator

from agent_bot.ai.contracts import AiModel, ResolvedModel, TokenUsage
from agent_bot.credential_values import CredentialSnapshot, CredentialValue
from agent_bot.media.contracts import MediaAssetView, ModelImage
from agent_bot.models import ChatMessage, LlmRequestOptions
from agent_bot.multimodal import DEFAULT_IMAGE_PROMPT

CODE_PATTERN = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
# 单服务白名单、发现结果及多服务合并共用上限，容纳 Windows MCP 的完整工具集。
MAX_MCP_TOOLS = 32


class DirectoryItem(AiModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    enabled: bool = False

    @field_validator("name")
    @classmethod
    def nonblank(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("invalid name")
        return value


class CodedItem(DirectoryItem):
    code: str

    @field_validator("code")
    @classmethod
    def valid_code(cls, value):
        if not CODE_PATTERN.fullmatch(value):
            raise ValueError("invalid code")
        return value


class AgentCreate(CodedItem):
    """Agent 聊天绑定与可选图片、错误转发路由，空值保持既有行为。"""

    system_prompt: str = Field(default="", max_length=32000)
    model_id: int = Field(ge=1)
    vision_model_id: int | None = Field(default=None, ge=1)
    fallback_model_id: int | None = Field(default=None, ge=1)
    skill_ids: tuple[int, ...] = Field(default=(), max_length=256)
    tools: tuple[str, ...] = Field(default=(), max_length=256)
    mcp_ids: tuple[int, ...] = Field(default=(), max_length=3)
    # 历史数据库兼容字段已弃用；执行预算统一来自全局机器人配置。
    max_steps: int = Field(default=3, ge=1, deprecated=True,
                           description="已弃用且执行时忽略，工具调用预算统一使用全局配置")
    options: LlmRequestOptions = Field(default_factory=LlmRequestOptions)

    @field_validator("skill_ids", "tools", "mcp_ids")
    @classmethod
    def unique(cls, value):
        if len(set(value)) != len(value):
            raise ValueError("duplicate selection")
        if any(isinstance(v, int) and (isinstance(v, bool) or v < 1) for v in value):
            raise ValueError("invalid identifier")
        if any(isinstance(v, str) and (not v.strip() or len(v) > 200) for v in value):
            raise ValueError("invalid tool name")
        return value

    @field_validator("tools")
    @classmethod
    def remove_retired_search(cls, value):
        """清理旧内置搜索授权；MCP 同名工具仍由关联服务的白名单独立开放。"""
        return tuple(name for name in value if name != "web_search")


class SkillCreate(DirectoryItem):
    category: str = Field(default="text", min_length=1, max_length=64)
    system_prompt: str = Field(default="", max_length=16000)
    sample_prompt: str = Field(default="", max_length=32000)
    tools: tuple[str, ...] = Field(default=(), max_length=256)

    _unique = field_validator("tools")(AgentCreate.unique.__func__)


class McpFields(CodedItem):
    """MCP 服务公开连接信息及服务端工具白名单。"""

    category: Literal["general", "search"] = "general"
    transport: Literal["streamable", "sse"] = "streamable"
    endpoint: str = Field(min_length=1, max_length=512)
    # 空集合显式关闭全部工具；星号表示允许发现结果中的全部工具。
    allowed_tools: tuple[str, ...] = Field(max_length=MAX_MCP_TOOLS)
    # 仅兼容旧数据库列，执行时不再读取该字段或改写 MCP 原生工具参数。
    search_tool: str | None = Field(default=None, min_length=1, max_length=100)
    timeout_seconds: int = Field(default=10, ge=1, le=30)
    required: bool = False

    _unique = field_validator("allowed_tools")(AgentCreate.unique.__func__)

    @field_validator("endpoint")
    @classmethod
    def valid_endpoint(cls, value):
        parts = urlsplit(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or any(ord(c) <= 32 or ord(c) == 127 for c in value)
        ):
            raise ValueError("invalid endpoint")
        if parts.port is not None and not 1 <= parts.port <= 65535:
            raise ValueError("invalid port")
        return value


class McpCreate(McpFields):
    """直接写入 MCP 令牌，编辑省略时保留原值。"""

    credential: CredentialValue | None = Field(default=None, repr=False)


class VersionedItem(AiModel):
    id: int
    revision: int
    created_at: datetime
    updated_at: datetime


class AgentView(AgentCreate, VersionedItem):
    pass


class SkillView(SkillCreate, VersionedItem):
    pass


class McpView(McpFields, CredentialSnapshot, VersionedItem):
    """目录公开状态与仅内部使用的密钥快照。"""


class DirectoryPatch(AiModel):
    expected_revision: int = Field(ge=1)


# Validate the merged object through its Create type, preserving omitted/null semantics.
def directory_patch(name, model):
    return create_model(
        name,
        __base__=DirectoryPatch,
        **{field: (info.annotation | None, None) for field, info in model.model_fields.items()},
    )


AgentUpdate = directory_patch("AgentUpdate", AgentCreate)
SkillUpdate = directory_patch("SkillUpdate", SkillCreate)
McpUpdate = directory_patch("McpUpdate", McpCreate)


class AgentRequest(AiModel):
    """内部请求将 QQ 图片地址与管理上传标识分开，禁止公开序列化。"""

    agent_code: str | None = None
    model: str | None = Field(default=None, max_length=200)
    prompt: str = Field(max_length=32000)
    image_urls: tuple[str, ...] = Field(default=(), max_length=8, exclude=True, repr=False)
    attachment_ids: tuple[str, ...] = Field(default=(), max_length=8, exclude=True, repr=False)
    image_material_ids: tuple[str, ...] = Field(default=(), max_length=8, exclude=True, repr=False)
    messages: tuple[ChatMessage, ...] = Field(default=(), max_length=100)
    session_id: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    tools: tuple[str, ...] | None = Field(default=None, max_length=256)
    mcp_services: tuple[str, ...] | None = Field(default=None, max_length=3)
    # 旧请求字段已弃用，既不收紧也不扩大全局工具调用预算。
    max_steps: int | None = Field(default=None, ge=1, deprecated=True,
                                  description="已弃用且执行时忽略，工具调用预算统一使用全局配置")
    options: LlmRequestOptions = Field(default_factory=LlmRequestOptions)
    timeout_seconds: float = Field(default=300, gt=0, le=3600, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_request(self):
        """只有本次附图允许空提示，远程、临时附件及持久素材三种来源不能混用。"""
        if sum(bool(value) for value in (
            self.image_urls, self.attachment_ids, self.image_material_ids,
        )) > 1:
            raise ValueError("conflicting media inputs")
        if any(not re.fullmatch(r"[a-f0-9]{32}", id)
               for id in (*self.attachment_ids, *self.image_material_ids)):
            raise ValueError("invalid attachment identifier")
        if not self.prompt.strip() and (
            self.image_urls or self.attachment_ids or self.image_material_ids
        ):
            object.__setattr__(self, "prompt", DEFAULT_IMAGE_PROMPT)
        if not self.prompt.strip() or sum(len(m.content) for m in self.messages) > 120000:
            raise ValueError("invalid conversation")
        if any(len(m.content) > 32000 for m in self.messages):
            raise ValueError("message too long")
        for values in (self.tools, self.mcp_services):
            if values is not None:
                AgentCreate.unique(values)
        for code in (self.agent_code, *(self.mcp_services or ())):
            if code is not None and not CODE_PATTERN.fullmatch(code):
                raise ValueError("invalid code")
        return self


class ExecutionAuthority(AiModel):
    entry: Literal["admin", "qq"]
    allowed_tools: tuple[str, ...] = ()
    allowed_mcp_services: tuple[str, ...] = ()
    chat_token: SecretStr | None = Field(default=None, exclude=True, repr=False)


class AgentSnapshot(AiModel):
    """聊天、识图和备用模型固定在同一目录事务，运行时不重读配置。"""

    agent: AgentView | None = None
    skills: tuple[SkillView, ...] = ()
    mcps: tuple[McpView, ...] = ()
    resolved: ResolvedModel
    vision: ResolvedModel | None = None
    fallback: ResolvedModel | None = None
    revisions: dict[str, int] = Field(default_factory=dict)


class ToolDefinition(AiModel):
    """工具来源决定可信参数校验，不由模型自行选择。"""

    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=16000)
    input_schema: dict[str, object]
    source: Literal["chat", "builtin", "mcp", "media"]


class ToolCall(AiModel):
    call_id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=200)
    arguments: dict[str, object]

    @field_validator("arguments")
    @classmethod
    def bounded_arguments(cls, value):
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode()) > 65536:
            raise ValueError("arguments too large")
        return value


class ToolResult(AiModel):
    """模型接收有界正文和私有图片，界面附件独立交付，图片字节不参与序列化。"""

    call_id: str
    name: str
    content: str = Field(max_length=32000)
    is_error: bool = False
    attachments: tuple[MediaAssetView, ...] = Field(default=(), exclude=True)
    images: tuple[ModelImage, ...] = Field(default=(), max_length=8, exclude=True, repr=False)


class TurnMessage(AiModel):
    """用户消息持有私有图片，仅供应商协议显式读取和编码。"""

    role: Literal["system", "user", "assistant", "tool"]
    text: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    tool_result: ToolResult | None = None
    continuation: tuple[dict, ...] = Field(default=(), exclude=True, repr=False)
    images: tuple[ModelImage, ...] = Field(default=(), max_length=8, exclude=True, repr=False)

    @model_validator(mode="after")
    def role_fields(self):
        """图片只能属于用户，工具与延续信息仍使用原角色约束。"""
        if self.images and self.role != "user":
            raise ValueError("images require user role")
        if bool(self.tool_result) != (self.role == "tool"):
            raise ValueError("tool result requires tool role")
        if self.tool_calls and self.role != "assistant":
            raise ValueError("tool calls require assistant role")
        return self


class AgentModelCall(AiModel):
    resolved: ResolvedModel
    credential: SecretStr | None = Field(default=None, exclude=True, repr=False)
    messages: tuple[TurnMessage, ...]
    tools: tuple[ToolDefinition, ...] = ()
    options: LlmRequestOptions = Field(default_factory=LlmRequestOptions)
    timeout_seconds: float = Field(gt=0, le=3600, allow_inf_nan=False)
    final_only: bool = False


class ModelTurn(AiModel):
    """模型单轮结果；调用数量由运行配置控制，不在协议层写死。"""

    content: str = ""
    calls: tuple[ToolCall, ...] = ()
    model: str = Field(min_length=1, max_length=255)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    continuation: tuple[dict, ...] = Field(default=(), exclude=True, repr=False)


class ModelEvent(AiModel):
    kind: Literal["text_delta", "turn_end"]
    delta: str | None = None
    turn: ModelTurn | None = None


class AgentCompletionMetadata(AiModel):
    """终态元数据包含已发布附件，不嵌入实际媒体内容。"""

    execution_id: str
    request_id: str
    model: str
    usage: TokenUsage
    duration_ms: int
    record_saved: bool
    backend: Literal["python-agent"] = "python-agent"
    attachments: tuple[MediaAssetView, ...] = ()


class AgentCompletion(AgentCompletionMetadata):
    content: str


class AgentEvent(AiModel):
    """附件事件必须携带已就绪元数据，避免把未提交文件误报为成果。"""

    kind: Literal[
        "start",
        "model_start",
        "tool_start",
        "tool_result",
        "warning",
        "text_delta",
        "done",
        "error",
        "artifact",
    ]
    execution_id: str
    sequence: int
    turn: int = 0
    delta: str | None = None
    tool: str | None = None
    call_id: str | None = None
    code: str | None = None
    message: str | None = None
    metadata: AgentCompletionMetadata | None = None
    duration_ms: int = 0
    status_code: int | None = None
    attachment: MediaAssetView | None = None
    # 原始工具负载仅交给记录存储，SSE 不公开这些内部字段。
    arguments: dict[str, object] | None = Field(default=None, exclude=True, repr=False)
    result: dict[str, object] | None = Field(default=None, exclude=True, repr=False)

    @model_validator(mode="after")
    def valid_attachment(self):
        """只有 artifact 事件可携带附件，且必须已发布为 ready。"""
        if self.kind == "artifact":
            if self.attachment is None or self.attachment.status != "ready":
                raise ValueError("artifact must be ready")
        elif self.attachment is not None:
            raise ValueError("unexpected artifact")
        return self


class ServiceWarning(AiModel):
    code: str
    service_code: str


class AgentReadiness(AiModel):
    initialized: bool
    agent_available: bool = False
    model_available: bool = False
    code: str | None = None


class RunSummary(AiModel):
    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    session_id: str = Field(min_length=1, max_length=256)
    request_id: str = Field(min_length=1, max_length=256)
    entry: Literal["admin", "qq"]
    model: str = Field(min_length=1, max_length=255)
    backend: Literal["python-agent"] = "python-agent"
    status: Literal["running", "completed", "failed", "cancelled"] = "running"
    duration_ms: int = Field(default=0, ge=0)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    created_at: datetime
    error_code: str | None = None


class RunDetail(RunSummary):
    """运行文本和独立查询的附件详情，附件不写入旧历史表。"""

    prompt: str = Field(max_length=32000)
    content: str = ""
    revisions: dict[str, int] = Field(default_factory=dict)
    attachments: tuple[MediaAssetView, ...] = ()


class RunStart(RunDetail):
    """执行开始时固定无凭据请求快照，仅保存到统一记录表。"""

    request_context: dict | None = None


class RunFinish(AiModel):
    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    status: Literal["completed", "failed", "cancelled"]
    content: str = ""
    model: str | None = None
    duration_ms: int = Field(default=0, ge=0)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    error_code: str | None = None


class StepWrite(AiModel):
    """保存工具调用的脱敏负载；旧步骤空值明确表示未记录。"""

    execution_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    sequence: int = Field(ge=1)
    turn: int = Field(ge=0)
    kind: str = Field(max_length=32)
    tool: str | None = Field(default=None, max_length=200)
    call_id: str | None = Field(default=None, max_length=256)
    status: Literal["started", "completed", "failed", "unknown"]
    duration_ms: int = Field(default=0, ge=0)
    summary: str = Field(default="", max_length=1000)
    arguments: dict[str, object] | None = None
    result: dict[str, object] | None = None


class StepView(StepWrite):
    pass
