"""请求级模型接管：显式备用只使用一次，不重新执行已完成的工具。"""

from agent_bot.agent.contracts import AgentModelCall, AgentSnapshot
from agent_bot.agent.errors import AgentError
from agent_bot.ai.contracts import ResolvedModel
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.errors import AiError
from agent_bot.ai.providers.langchain import LangChainProvider
from agent_bot.models import LlmRequestOptions
from agent_bot.services.request_deadline import remaining_timeout

# 仅模型配置、上游和响应错误可接管；权限、工具、取消及总期限均不在此列。
FORWARDABLE_ERRORS = frozenset(
    {
        "AGENT_MODEL_UNAVAILABLE",
        "MEDIA_VISION_UNSUPPORTED",
        "AI_CREDENTIAL_MISSING",
        "AI_OPTION_UNSUPPORTED",
        "AI_RATE_LIMITED",
        "AI_UPSTREAM_AUTH_REJECTED",
        "AI_UPSTREAM_UNAVAILABLE",
        "AI_UPSTREAM_REJECTED",
        "AI_TIMEOUT",
        "AI_RESPONSE_INVALID",
        "AI_RESPONSE_TOO_LARGE",
        "AGENT_MODEL_RESPONSE_INVALID",
        "AGENT_MODEL_RESPONSE_TOO_LARGE",
    }
)


class AgentModelRouter:
    """持有一致性目录快照、已选模型和一次接管额度，复用整个请求总预算。"""

    def __init__(
        self,
        snapshot: AgentSnapshot,
        providers: dict[str, LangChainProvider],
        credentials: CredentialResolver,
        options: LlmRequestOptions,
        *,
        has_images: bool,
        timeout_seconds: float,
    ) -> None:
        """备用模型凭据和状态仅在接管时校验，不影响成功的主模型。"""
        self.snapshot, self.providers, self.credentials = snapshot, providers, credentials
        self.options, self.has_images, self.timeout_seconds = options, has_images, timeout_seconds
        self.resolved = snapshot.resolved
        self.credential = None
        self.forwarded = False
        self.initial_warning: str | None = None
        try:
            self._bind(self.resolved)
        except AiError as error:
            self.initial_warning = self.forward(error)

    def _bind(self, resolved: ResolvedModel) -> None:
        """绑定前完整校验模型，禁止转发时移除图像或悄悄修改生成参数。"""
        if not resolved.model.enabled or not resolved.provider.enabled:
            raise AgentError("AGENT_MODEL_UNAVAILABLE", status_code=400)
        if self.has_images and not resolved.model.media.vision_enabled:
            raise AgentError("MEDIA_VISION_UNSUPPORTED", status_code=422)
        credential = self.credentials.resolve(resolved.provider)
        self.providers[resolved.provider.protocol].validate_options(self.options)
        self.resolved, self.credential = resolved, credential

    def forward(self, error: AiError) -> str:
        """仅明确允许的模型故障接管一次，剩余期限耗尽则立即退出。"""
        if self.forwarded or not self.snapshot.fallback or error.code not in FORWARDABLE_ERRORS:
            raise error
        remaining_timeout(self.timeout_seconds)
        previous = self.resolved.model.alias
        self.forwarded = True
        self._bind(self.snapshot.fallback)
        return f"{error.code}：{previous} 转交 {self.resolved.model.alias}"

    @property
    def provider(self) -> LangChainProvider:
        """协议来自当前模型绑定，不能由模型生成的工具参数指定。"""
        return self.providers[self.resolved.provider.protocol]

    def bind_call(self, call: AgentModelCall) -> AgentModelCall:
        """工具首次返回图片时只使用显式识图路由，跨模型前清除旧模型私有延续。"""
        messages = call.messages
        if not self.has_images and any(
            message.images or (message.tool_result and message.tool_result.images)
            for message in messages
        ):
            self.has_images = True
            previous = self.resolved.model.id
            target = self.snapshot.vision if not self.forwarded else None
            self._bind(target or self.resolved)
            if self.resolved.model.id != previous:
                messages = tuple(
                    message.model_copy(update={"continuation": ()}) for message in messages
                )
        return call.model_copy(
            update={
                "messages": messages,
                "resolved": self.resolved,
                "credential": self.credential,
                "timeout_seconds": remaining_timeout(self.timeout_seconds),
            }
        )
