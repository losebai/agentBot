"""按明确配置选择已迁移能力，任何执行失败都不触发后端回退。"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import SecretStr

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority
from agent_bot.agent.tools.policy import LEGACY_TOOLS, MEDIA_TOOLS
from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.context import format_qq_context
from agent_bot.ai.contracts import TextRequest
from agent_bot.ai.errors import AiError
from agent_bot.ai.service import AiService
from agent_bot.integrations.dispatch_ai import (
    DispatchAiClient,
    DispatchAiProtocolError,
    DispatchAiRejectedError,
    DispatchAiUnavailableError,
)
from agent_bot.media.contracts import MediaOwner
from agent_bot.model_guard import ModelGuard, ModelGuardUnavailableError, ModelNotAvailableError
from agent_bot.models import (
    ChatContext,
    ChatMediaRequest,
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

    backend: Literal["java", "python"]
    capability: Literal["ordinary-text", "agent", "legacy", "media", "media-image", "media-audio"]
    reason: str


def select_route(
    request: AiRouteRequest, *, plain_backend="java", agent_backend="java", media_backend="java"
):
    """未迁移能力整条走 Java；混合媒体 Agent 同时受两个开关控制。"""
    if (
        request.videos
        or request.laya
        or set(request.chat_tools) & LEGACY_TOOLS
        or "laya" in request.mcp_services
    ):
        return RouteDecision("java", "legacy", "请求包含尚未迁移的能力")
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
        if media_backend != "python" or (requires_agent and agent_backend != "python"):
            return RouteDecision("java", "legacy", "媒体或混合 Agent 后端配置为 Java")
        return RouteDecision(
            "python", "agent" if requires_agent else "media", "使用 Python 媒体能力"
        )
    if requires_agent:
        return RouteDecision(agent_backend, "agent", "使用配置的 Agent 后端")
    return RouteDecision(plain_backend, "ordinary-text", "使用配置的普通文本后端")


def select_backend(request: AiRouteRequest, plain_backend: Literal["java", "python"]):
    return (
        "java"
        if any(
            (
                request.agent,
                request.web,
                request.tools,
                request.mcp,
                request.laya,
                request.images,
                request.videos,
            )
        )
        else plain_backend
    )


class RoutedModelGuard:
    """按实际路由检查对应目录，避免拿 Python 别名访问 Java 模型表。"""

    def __init__(
        self,
        java_guard: ModelGuard,
        catalog: AiCatalog,
        *,
        plain_backend: str,
        media_backend="java",
    ):
        """保留独立的纯文本和媒体选择。"""
        self.java_guard, self.catalog, self.plain_backend = java_guard, catalog, plain_backend
        self.media_backend = media_backend

    async def ensure_enabled(self, model_id: str, *, capability: str = "java") -> None:
        """直接媒体生成检查显式能力，Agent 和识图由其一致快照预检。"""
        if (capability == "ordinary-text" and self.plain_backend == "python") or (
            capability in {"media-image", "media-audio"} and self.media_backend == "python"
        ):
            try:
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
                exception = (
                    ModelGuardUnavailableError
                    if error.status_code == 503
                    else ModelNotAvailableError
                )
                raise exception(error.code) from error
        else:
            await self.java_guard.ensure_enabled(model_id)


class RoutingAiClient:
    """按能力开关选择服务，选中 Python 后失败不自动回退 Java。"""
    def __init__(
        self,
        java_client: DispatchAiClient,
        python_service: AiService,
        *,
        plain_backend: Literal["java", "python"],
        timeout_seconds: float,
        default_options: LlmRequestOptions,
        mcp_services: Sequence[str],
        laya_enabled: bool,
        agent_service=None,
        agent_backend: Literal["java", "python"] = "java",
        agent_code: str | None = None,
        agent_max_steps: int | None = None,
        web_search_max_steps: int | None = None,
        media_backend: Literal["java", "python"] = "java",
        media_service=None,
    ):
        """媒体服务和开关随运行时快照更新，存储资源由进程持有。"""
        self.java_client, self.python_service = java_client, python_service
        self.plain_backend, self.timeout_seconds = plain_backend, min(timeout_seconds, 300)
        self.default_options, self.mcp_services, self.laya_enabled = (
            default_options,
            mcp_services,
            laya_enabled,
        )
        self.agent_service, self.agent_backend, self.agent_code = (
            agent_service,
            agent_backend,
            agent_code,
        )
        self.agent_max_steps, self.web_search_max_steps = agent_max_steps, web_search_max_steps
        self.media_backend, self.media_service = media_backend, media_service

    def route(self, request: AiRouteRequest) -> RouteDecision:
        """加入配置的高级能力后统一判定，预检和执行都使用此入口。"""
        if self.laya_enabled and (request.tools or request.chat_tools):
            from dataclasses import replace

            request = replace(request, laya=True)
        if request.agent and self.mcp_services:
            from dataclasses import replace

            request = replace(request, mcp_services=tuple(self.mcp_services))
        return select_route(
            request,
            plain_backend=self.plain_backend,
            agent_backend=self.agent_backend,
            media_backend=self.media_backend,
        )

    def media_route(self, output_type):
        """直接生成不依赖 Agent 开关，模型预检使用同一媒体后端。"""
        if output_type not in {"IMAGE", "AUDIO"}:
            raise ValueError("invalid media output")
        return RouteDecision(
            self.media_backend,
            "media-image" if output_type == "IMAGE" else "media-audio",
            "使用配置的媒体后端",
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
        video_urls: Sequence[str] = (),
        use_agent: bool = True,
        use_web_search: bool = True,
        request_options: LlmRequestOptions | None = None,
        chat_tools: Sequence[ChatToolName] = (),
        tool_session_token: str | None = None,
        laya_decision: bool = False,
    ) -> str:
        """将 QQ 图片传给 Python Agent，纯识图强制不启用工具或预设。"""
        route = AiRouteRequest(
            agent=use_agent,
            web=use_web_search,
            tools=bool(chat_tools),
            mcp=bool(self.mcp_services and use_agent),
            laya=laya_decision,
            images=bool(image_urls),
            videos=bool(video_urls),
            chat_tools=tuple(chat_tools),
        )
        decision = self.route(route)
        if decision.backend == "java":
            return await self.java_client.complete(
                model=model,
                session_id=session_id,
                prompt=prompt,
                messages=messages,
                request_id=request_id,
                user=user,
                chat_context=chat_context,
                image_urls=image_urls,
                video_urls=video_urls,
                use_agent=use_agent,
                use_web_search=use_web_search,
                request_options=request_options,
                chat_tools=chat_tools,
                tool_session_token=tool_session_token,
                laya_decision=laya_decision,
            )
        if tool_session_token and decision.capability == "ordinary-text":
            raise ValueError("ordinary text cannot carry tool credentials")
        try:
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
                agent_code = None if vision_only else self.agent_code
                request = AgentRequest(
                    agent_code=agent_code,
                    model=None if agent_code else model,
                    image_urls=tuple(image_urls),
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
            if error.status_code in {503, 504, 429}:
                exception = DispatchAiUnavailableError
            elif error.code.startswith("AI_RESPONSE"):
                exception = DispatchAiProtocolError
            else:
                exception = DispatchAiRejectedError
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
    ):
        """媒体默认保留 Java；显式选择 Python 后只调用一次对应生成用例。"""
        if self.media_backend == "python":
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
                    DispatchAiUnavailableError
                    if error.status_code in {503, 504, 429}
                    else DispatchAiRejectedError
                )
                raise exception(
                    error.code, status_code=error.status_code, error_code=error.code
                ) from error
        return await self.java_client.generate_media(
            model=model,
            session_id=session_id,
            prompt=prompt,
            messages=messages,
            request_id=request_id,
            output_type=output_type,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            image_format=image_format,
            voice=voice,
            audio_format=audio_format,
            user=user,
        )

    async def decide_auto_speak(self, *, state: str, session_id: str, request_id: str):
        return await self.java_client.decide_auto_speak(
            state=state, session_id=session_id, request_id=request_id
        )

    async def pending_jobs(self, bot_id: str, after_id: str | None = None):
        return await self.java_client.pending_jobs(bot_id, after_id)

    async def acknowledge_job(self, bot_id: str, job_id: str) -> None:
        await self.java_client.acknowledge_job(bot_id, job_id)

    async def aclose(self):
        """关闭当前快照拥有的模型连接，进程媒体存储由外层最后释放。"""
        try:
            try:
                try:
                    if self.agent_service is not None:
                        await self.agent_service.aclose()
                finally:
                    if self.media_service is not None:
                        await self.media_service.aclose()
            finally:
                await self.python_service.aclose()
        finally:
            await self.java_client.aclose()
