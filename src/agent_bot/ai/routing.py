"""选择本地 AI 能力并保留请求授权和资源释放边界。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority
from agent_bot.agent.errors import PARTIAL_REPLY_ERRORS, AgentError
from agent_bot.agent.tools.policy import MEDIA_TOOLS
from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.chat_errors import (
    ChatAiProtocolError,
    ChatAiRejectedError,
    ChatAiUnavailableError,
)
from agent_bot.ai.context import format_qq_context
from agent_bot.ai.contracts import TextRequest
from agent_bot.ai.errors import AiError
from agent_bot.ai.service import AiService
from agent_bot.media.contracts import MediaOwner
from agent_bot.model_guard import ModelGuardUnavailableError, ModelNotAvailableError
from agent_bot.models import (
    ChatContext,
    ChatMediaRequest,
    ChatMediaResponse,
    ChatMessage,
    ChatToolName,
    LlmRequestOptions,
    MediaOutputType,
)


@dataclass(frozen=True)
class AiRouteRequest:
    agent: bool = False
    web: bool = False
    tools: bool = False
    mcp: bool = False
    laya: bool = False
    images: bool = False
    videos: bool = False
    chat_tools: tuple[str, ...] = ()
    mcp_services: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteDecision:
    """预检与实际调用共享同一能力决策。"""

    backend: Literal["python"]
    capability: Literal["ordinary-text", "agent", "media", "media-image", "media-audio"]
    reason: str


def select_route(request: AiRouteRequest) -> RouteDecision:
    """仅选择本地已实现的能力，未迁移请求在调用模型前明确拒绝。"""
    if (
        request.laya
        or "soloncode" in request.chat_tools
        or "laya" in request.mcp_services
    ):
        raise AiError("AI_CAPABILITY_UNSUPPORTED", status_code=422)
    requires_agent = any(
        (
            request.agent,
            request.web,
            request.tools,
            request.mcp,
            request.chat_tools,
            request.mcp_services,
        )
    )
    if request.images or set(request.chat_tools) & MEDIA_TOOLS:
        return RouteDecision("python", "agent" if requires_agent else "media", "使用媒体能力")
    return RouteDecision("python", "agent" if requires_agent else "ordinary-text", "使用本地 AI")


class RoutedModelGuard:
    """统一使用本地模型目录，媒体调用同时检查媒体能力。"""

    def __init__(
        self, catalog: AiCatalog, *, agent_catalog=None, agent_code: str | None = None,
    ) -> None:
        """预设 Agent 使用其绑定模型；未使用预设时检查内容路由选择的模型。"""
        self.catalog = catalog
        self.agent_catalog = agent_catalog
        self.agent_code = agent_code

    async def ensure_enabled(self, model_id: str, *, capability: str = "ordinary-text") -> None:
        """入队前确认所选模型存在，执行时仍通过一致快照检查完整能力。"""
        try:
            if capability == "agent" and self.agent_code:
                snapshot = await self.agent_catalog.snapshot(AgentRequest(
                    agent_code=self.agent_code, prompt="model-preflight",
                    session_id="model-preflight", request_id="model-preflight",
                    mcp_services=(),
                ))
                resolved = snapshot.resolved
            else:
                resolved = await self.catalog.resolve(model_id)
            if capability.startswith("media-"):
                media = resolved.model.media
                if not resolved.model.media_initialized:
                    raise AiError("MEDIA_NOT_INITIALIZED", status_code=503)
                if (
                    resolved.provider.protocol == "ollama"
                    or (capability == "media-image" and media.image_api_profile == "none")
                    or (capability == "media-audio" and not media.audio_chat_enabled)
                ):
                    raise AiError("MEDIA_CAPABILITY_UNSUPPORTED", status_code=422)
        except AiError as error:
            # 预设、工具及媒体配置拒绝不能被误标成模型缺失。
            if error.status_code != 503 and error.code not in {
                "AI_MODEL_NOT_FOUND", "AI_MODEL_DISABLED", "AI_PROVIDER_DISABLED",
                "AGENT_MODEL_UNAVAILABLE",
            }:
                raise ChatAiRejectedError(
                    error.code, error_code=error.code, status_code=error.status_code,
                ) from error
            exception = (
                ModelGuardUnavailableError
                if error.status_code == 503
                else ModelNotAvailableError
            )
            raise exception(error.code) from error


class RoutingAiClient:
    """统一调用本地文本、Agent 和媒体服务。"""

    def __init__(
        self,
        python_service: AiService,
        *,
        timeout_seconds: float,
        default_options: LlmRequestOptions,
        mcp_services: Sequence[str],
        agent_service=None,
        agent_code: str | None = None,
        agent_max_steps: int | None = None,
        web_search_max_steps: int | None = None,
        media_service=None,
        job_service=None,
        legacy_system_prompt: str | None = None,
    ) -> None:
        """服务随运行时快照更新，媒体存储由进程负责释放。"""
        self.python_service = python_service
        self.timeout_seconds = min(timeout_seconds, 300)
        self.default_options, self.mcp_services = default_options, mcp_services
        self.agent_service, self.agent_code = agent_service, agent_code
        self.agent_max_steps, self.web_search_max_steps = agent_max_steps, web_search_max_steps
        self.media_service = media_service
        self.job_service = job_service
        self.legacy_system_prompt = legacy_system_prompt

    async def current_system_prompt(self) -> str | None:
        """按当前预设读取人设；空字符串是明确清空，只有未选择 Agent 时返回 None。"""
        if not self.agent_code:
            return None
        if self.agent_service is None:
            raise AiError("AGENT_NOT_INITIALIZED", status_code=503)
        return await self.agent_service.catalog.get_system_prompt(self.agent_code)

    async def _system_messages(self, messages: Sequence[ChatMessage]) -> tuple[ChatMessage, ...]:
        """系统调用继承 Agent 人设并保留任务约束，不带入技能、工具或历史会话。"""
        role = await self.current_system_prompt()
        if role is None:
            return tuple(messages)
        result = [ChatMessage(role="system", content=role)] if role.strip() else []
        result.extend(
            message for message in messages
            if not (message.role == "system" and self.legacy_system_prompt is not None
                    and message.content.strip() == self.legacy_system_prompt.strip())
        )
        return tuple(result)

    async def run_due_jobs(self):
        """执行一次本地到期任务，调度器负责功能开关与运行时租约。"""
        if self.job_service is not None:
            await self.job_service.run_due()

    async def pending_jobs(self, bot_id, after=None):
        """结果读取不确认消费，已生成正文由持久 QQ 队列投递。"""
        if self.job_service is None:
            raise AiError("JOB_NOT_INITIALIZED", status_code=503)
        return await self.job_service.pending_jobs(bot_id, after)

    async def acknowledge_job(self, bot_id, job_id):
        """关闭开关后仍允许确认已完成的在途投递。"""
        if self.job_service is None:
            raise AiError("JOB_NOT_INITIALIZED", status_code=503)
        try:
            await self.job_service.acknowledge_job(bot_id, job_id)
        except AiError as error:
            # 数据库确认故障沿用队列的可重试类别，已发送的分段不再重发。
            exception = ChatAiUnavailableError if error.status_code >= 500 else ChatAiRejectedError
            raise exception(error.code, status_code=error.status_code,
                            error_code=error.code) from error

    def route(self, request: AiRouteRequest) -> RouteDecision:
        """加入已授权的 MCP 服务，预检和执行共享能力判断。"""
        if request.agent and self.mcp_services:
            from dataclasses import replace

            request = replace(request, mcp_services=tuple(self.mcp_services))
        return select_route(request)

    def media_route(self, output_type: MediaOutputType) -> RouteDecision:
        """直接生成独立于 Agent，预检检查实际媒体类型。"""
        if output_type not in {"IMAGE", "AUDIO"}:
            raise ValueError("invalid media output")
        return RouteDecision(
            "python",
            "media-image" if output_type == "IMAGE" else "media-audio",
            "使用本地媒体能力",
        )

    async def complete(
        self,
        *,
        model: str,
        session_id: str,
        prompt: str,
        messages: Sequence[ChatMessage],
        request_id: str,
        user: str | None = None,
        chat_context: ChatContext | None = None,
        image_urls: Sequence[str] = (),
        image_material_ids: Sequence[str] = (),
        video_urls: Sequence[str] = (),
        use_agent: bool = True,
        use_web_search: bool = True,
        request_options: LlmRequestOptions | None = None,
        chat_tools: Sequence[ChatToolName] = (),
        tool_session_token: str | None = None,
        laya_decision: bool = False,
        allow_knowledge: bool = True,
    ) -> str:
        """系统调用继承当前 Agent 人设，模型及工具授权仍由原路由决定。"""
        route = AiRouteRequest(
            agent=use_agent,
            web=use_web_search,
            tools=bool(chat_tools),
            mcp=bool(self.mcp_services and use_agent),
            laya=laya_decision,
            images=bool(image_urls or image_material_ids),
            videos=bool(video_urls),
            chat_tools=tuple(chat_tools),
        )
        decision = self.route(route)
        if tool_session_token and decision.capability == "ordinary-text":
            raise ValueError("ordinary text cannot carry tool credentials")
        try:
            messages = tuple(messages) or (ChatMessage(role="user", content=prompt),)
            if decision.capability in {"ordinary-text", "media"}:
                messages = await self._system_messages(messages)
            if decision.capability in {"agent", "media"}:
                if self.agent_service is None:
                    raise AiError("AGENT_NOT_INITIALIZED", status_code=503)
                vision_only = decision.capability == "media"
                names = (
                    ()
                    if vision_only
                    else tuple(chat_tools) + (("web_search", "read_url") if use_web_search else ())
                )
                services = tuple(self.mcp_services) if use_agent and not vision_only else ()
                # 共享知识仍与命名 Agent 的显式工具配置取交集，不改变普通对话路径。
                if allow_knowledge and use_agent and self.agent_code and not vision_only:
                    names += ("knowledge_search", "material_search")
                agent_code = None if vision_only else self.agent_code
                request = AgentRequest(
                    agent_code=agent_code,
                    model=None if agent_code else model,
                    image_urls=tuple(image_urls),
                    image_material_ids=tuple(image_material_ids),
                    tools=() if vision_only else None,
                    session_id=session_id,
                    prompt=prompt,
                    request_id=request_id,
                    messages=format_qq_context(
                        messages or (ChatMessage(role="user", content=prompt),), chat_context
                    ),
                    mcp_services=services,
                    max_steps=self.agent_max_steps if use_agent else self.web_search_max_steps,
                    options=request_options or self.default_options,
                    timeout_seconds=self.timeout_seconds,
                )
                authority = ExecutionAuthority(
                    entry="qq",
                    allowed_tools=names,
                    allowed_mcp_services=services,
                    chat_token=SecretStr(tool_session_token) if tool_session_token else None,
                )
                return (await self.agent_service.complete(request, authority)).content
            request = TextRequest(
                model=model,
                session_id=session_id,
                prompt=prompt,
                request_id=request_id,
                messages=format_qq_context(
                    messages or (ChatMessage(role="user", content=prompt),), chat_context
                ),
                options=request_options or self.default_options,
                timeout_seconds=self.timeout_seconds,
            )
            return (await self.python_service.complete(request)).content
        except AiError as error:
            # 仅采用本次 QQ 请求实际产生的文字，失败记录仍由 Agent 保存。
            if (
                isinstance(error, AgentError) and error.code in PARTIAL_REPLY_ERRORS
                and error.partial_content.strip()
            ):
                return error.partial_content
            if error.status_code in {503, 504, 429}:
                exception = ChatAiUnavailableError
            elif error.code.startswith("AI_RESPONSE"):
                exception = ChatAiProtocolError
            else:
                exception = ChatAiRejectedError
            raise exception(
                error.code, status_code=error.status_code, error_code=error.code
            ) from error

    async def generate_media(
        self,
        *,
        model: str,
        session_id: str,
        prompt: str | None,
        messages: Sequence[ChatMessage],
        request_id: str,
        output_type: MediaOutputType,
        timeout_seconds: float,
        max_bytes: int,
        image_format: str | None,
        voice: str | None,
        audio_format: str | None,
        user: str,
    ) -> ChatMediaResponse:
        """通过本地媒体服务生成一次结果，失败直接返回清晰错误。"""
        try:
            if self.media_service is None:
                raise AiError("MEDIA_NOT_INITIALIZED", status_code=503)
            request = ChatMediaRequest(
                model=model,
                session_id=session_id,
                prompt=prompt,
                messages=tuple(messages),
                request_id=request_id,
                output_type=output_type,
                timeout_seconds=max(1, int(min(timeout_seconds, 120))),
                max_bytes=max_bytes,
                image_format=image_format,
                voice=voice,
                audio_format=audio_format,
                user=user,
            )
            result = await self.media_service.generate(
                request, MediaOwner(entry="qq", session_id=session_id)
            )
            return result.response
        except AiError as error:
            exception = (
                ChatAiUnavailableError
                if error.status_code in {503, 504, 429}
                else ChatAiRejectedError
            )
            raise exception(
                error.code, status_code=error.status_code, error_code=error.code
            ) from error

    async def aclose(self) -> None:
        """释放当前快照的客户端，进程媒体存储由外层最后释放。"""
        try:
            try:
                if self.agent_service is not None:
                    await self.agent_service.aclose()
            finally:
                if self.media_service is not None:
                    await self.media_service.aclose()
        finally:
            await self.python_service.aclose()
