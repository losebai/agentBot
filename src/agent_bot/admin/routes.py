"""安装按可选管理员密码控制访问的管理接口，统一处理脱敏错误和运行状态。"""

import asyncio
import uuid
from contextlib import aclosing
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Header, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from agent_bot.admin.agent_routes import install_agent_routes
from agent_bot.admin.ai_routes import install_ai_routes
from agent_bot.admin.auth import require_token
from agent_bot.admin.chat import ChatProbe
from agent_bot.admin.config import AdminConfig
from agent_bot.admin.errors import classify_api_error
from agent_bot.admin.http import ManagementMiddleware
from agent_bot.admin.knowledge_routes import install_knowledge_routes
from agent_bot.admin.media_routes import install_media_routes
from agent_bot.admin.models import (
    ApiError,
    BotInfo,
    ChatRequest,
    ChatResponse,
    OverviewStatistics,
    ServiceStatus,
    SettingsSnapshot,
    SettingsUpdate,
)
from agent_bot.admin.napcat_routes import install_napcat_routes
from agent_bot.admin.plugin_routes import install_plugin_routes
from agent_bot.admin.queue_routes import install_queue_routes
from agent_bot.admin.settings import SettingsService
from agent_bot.admin.streaming import ManagedStreamingResponse
from agent_bot.admin.validation import describe_validation_error
from agent_bot.admin.workflow_routes import install_workflow_routes
from agent_bot.ai.call_records import CallRecordStore
from agent_bot.ai.chat_errors import ChatAiError, ChatAiUnavailableError
from agent_bot.ai.errors import AiError
from agent_bot.core.lifecycle import RuntimeRefreshInProgressError
from agent_bot.integrations.workflow_store import (
    WorkflowNotFoundError,
    WorkflowRevisionConflictError,
    WorkflowStoreError,
)
from agent_bot.model_guard import ModelGuardUnavailableError, ModelNotAvailableError
from agent_bot.services.workflows import WorkflowValidationError
from agent_bot.settings import (
    SettingsError,
    SettingsNotInitializedError,
    SettingsRevisionConflictError,
    SettingsValidationError,
)


def install_admin(app: FastAPI, store, runtime, admin: AdminConfig) -> None:
    """复用现有运行时装配管理端点，不创建第二个机器人实例。"""
    manager = SettingsService(
        store, runtime, startup_settings_store=getattr(runtime, "startup_settings_store", None),
    )
    chat = ChatProbe(runtime)

    async def authorize(authorization: Annotated[str | None, Header()] = None) -> None:
        """所有管理操作共享启动密码校验，仅空密码模式允许匿名访问。"""
        require_token(authorization, admin.token.get_secret_value())

    router = APIRouter(prefix="/api/v1", dependencies=[Depends(authorize)])
    install_queue_routes(router, runtime)
    from agent_bot.napcat.contracts import NapCatStatus

    def napcat_snapshot() -> NapCatStatus:
        """仅桌面提供所属托管状态，普通服务不探测外部 NapCat。"""
        supervisor = getattr(app.state, "napcat", None)
        return supervisor.snapshot() if supervisor is not None else NapCatStatus()

    install_napcat_routes(router, napcat_snapshot)

    @app.get("/api/v1/auth")
    async def auth_mode() -> dict[str, bool]:
        """公开登录模式供页面选择入口，仅返回是否需要密码，不返回凭据。"""
        return {"password_required": bool(admin.token.get_secret_value())}

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        """Liveness does not disclose settings or require a connected QQ account."""
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready() -> JSONResponse:
        """容器就绪探针只暴露队列状态，不调用模型或返回配置与凭据。"""
        try:
            available = await asyncio.wait_for(runtime.is_ready(), timeout=2)
        except Exception:
            available = False
        return JSONResponse(
            {"status": "ready" if available else "unavailable"},
            status_code=200 if available else 503,
        )

    @router.get("/settings", response_model=SettingsSnapshot)
    async def settings() -> SettingsSnapshot:
        """Load and redact persistent configuration off the event loop."""
        return await asyncio.to_thread(manager.read)

    @router.patch("/settings", response_model=SettingsSnapshot)
    async def update_settings(update: SettingsUpdate) -> SettingsSnapshot:
        """动态值保存数据库、启动值保存文件；进程与连接只能由用户手动重启切换。"""
        return await asyncio.to_thread(manager.update, update)

    @router.get("/bots", response_model=list[BotInfo])
    async def bots() -> list[BotInfo]:
        """List observed bot connections, without querying external profiles."""
        return runtime.bots()

    @router.get("/status", response_model=ServiceStatus)
    async def status() -> ServiceStatus:
        """在运行时租约内聚合归档并读取活动请求，保持热刷新和数据库释放边界。"""
        snapshot = await asyncio.to_thread(manager.read)
        async with runtime.lease():
            config = runtime.config
            service = getattr(runtime, "ai_service", None)
            python_ai = (
                await service.readiness(config.chat_model) if service is not None else None
            )
            agent_service = getattr(runtime, "agent_service", None)
            python_agent = (
                await agent_service.readiness(config.chat_python_agent_code)
                if agent_service
                else None
            )
            media_resources = getattr(runtime, "media_resources", None)
            python_media = media_resources.readiness if media_resources is not None else None
            statistics = None
            statistics_error = None
            if service is not None:
                sessions = service.sessions
                try:
                    totals = await CallRecordStore(
                        sessions.params, connect=sessions.connect
                    ).statistics()
                except AiError as error:
                    # 旧部署可能尚未升级归档表；明确报告统计失败，保留独立服务就绪信息。
                    statistics_error = error.code
                else:
                    statistics = OverviewStatistics(
                        **totals.model_dump(),
                        running_requests=service.active_requests + (
                            agent_service.active_requests if agent_service is not None else 0
                        ),
                    )
        return ServiceStatus(
            ready=await runtime.is_ready(),
            bot_count=len(runtime.bots()),
            database_revision=snapshot.database_revision,
            applied_revision=snapshot.applied_revision,
            restart_required_fields=snapshot.restart_required_fields,
            configured_model=config.chat_model,
            python_ai=python_ai,
            configured_agent=config.chat_python_agent_code,
            python_agent=python_agent,
            job_enabled=config.chat_job_enabled,
            auto_speak_enabled=config.chat_auto_speak_enabled,
            python_media=python_media,
            statistics=statistics,
            statistics_error=statistics_error,
        )

    @router.post("/chat", response_model=ChatResponse)
    async def complete(body: ChatRequest, request: Request) -> ChatResponse:
        """Perform a bounded text test using only the configured AI destination."""
        return await chat.complete(body, request.state.request_id)

    @router.post("/chat/stream")
    async def stream(body: ChatRequest, request: Request):
        """输出本地文本事件，连接取消交给统一的流式响应生命周期。"""

        async def content():
            async with aclosing(chat.stream(body, request.state.request_id)) as events:
                async for event in events:
                    data = event.model_dump_json(exclude_none=True)
                    yield f"event: {event.kind}\ndata: {data}\n\n"

        return ManagedStreamingResponse(
            content(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    install_ai_routes(router, runtime)
    install_knowledge_routes(router, runtime)
    install_plugin_routes(router, runtime, manager)
    from agent_bot.plugins.bilibili.routes import install_bilibili_routes

    install_bilibili_routes(router, runtime)
    install_agent_routes(router, runtime)
    install_media_routes(router, runtime)
    install_workflow_routes(router, runtime, manager)

    app.include_router(router)
    app.add_middleware(ManagementMiddleware)

    async def error_response(request: Request, error: Exception) -> JSONResponse:
        """按异常类型返回脱敏错误，不回显请求、密码或上游异常原文。"""
        if isinstance(error, HTTPException) and not request.url.path.startswith("/api/"):
            return await http_exception_handler(request, error)
        if isinstance(error, RequestValidationError) and not request.url.path.startswith("/api/"):
            return await request_validation_exception_handler(request, error)
        status_code, code, message = 500, "INTERNAL_ERROR", "服务暂时无法完成请求"
        if isinstance(error, AiError):
            status_code, code, message = classify_api_error(error)
        elif isinstance(error, HTTPException):
            status_code = error.status_code
            code, message = (
                ("UNAUTHORIZED", "管理密码无效")
                if status_code == 401
                else ("REQUEST_REJECTED", "请求无法处理")
            )
        elif isinstance(error, SettingsRevisionConflictError):
            status_code, code, message = 409, "SETTINGS_CONFLICT", "配置已被修改，请重新加载"
        elif isinstance(error, WorkflowRevisionConflictError):
            status_code, code, message = (
                409, "WORKFLOW_CONFLICT", "工作流已有更新，请重新加载后操作",
            )
        elif isinstance(error, WorkflowNotFoundError):
            status_code, code, message = 404, "WORKFLOW_NOT_FOUND", "工作流不存在"
        elif isinstance(error, WorkflowValidationError):
            status_code, code, message = (
                422, "WORKFLOW_INVALID", "工作流文件、节点绑定或执行状态不符合要求",
            )
        elif isinstance(error, WorkflowStoreError):
            status_code, code, message = (
                503, "WORKFLOW_UNAVAILABLE", "工作流数据库不可用，请检查连接及初始化结构",
            )
        elif isinstance(error, RuntimeRefreshInProgressError):
            status_code, code, message = (
                503, "RUNTIME_REFRESHING", "配置正在更新，请稍后刷新页面",
            )
        elif isinstance(error, SettingsNotInitializedError):
            status_code, code, message = 503, "SETTINGS_NOT_INITIALIZED", "配置数据库尚未初始化"
        elif isinstance(error, (ValidationError, RequestValidationError, SettingsValidationError)):
            status_code, code = 422, "INVALID_REQUEST"
            message = describe_validation_error(error, request)
        elif isinstance(error, SettingsError):
            status_code, code, message = 503, "SETTINGS_UNAVAILABLE", "配置数据库暂时不可用"
        elif isinstance(error, (ChatAiUnavailableError, ModelGuardUnavailableError)):
            status_code, code, message = 503, "AI_UNAVAILABLE", "AI 或模型目录暂时不可用"
        elif isinstance(error, ModelNotAvailableError):
            status_code, code, message = 400, "MODEL_DISABLED", "所选模型未登记或已停用"
        elif isinstance(error, ChatAiError):
            status_code, code, message = 502, "AI_REJECTED", "AI 服务未返回有效结果"
        request_id = getattr(request.state, "request_id", uuid.uuid4().hex)
        body = ApiError(code=code, message=message, request_id=request_id)
        headers = {"WWW-Authenticate": "Bearer"} if status_code == 401 else None
        return JSONResponse(body.model_dump(), status_code=status_code, headers=headers)

    for exception_type in (
        HTTPException,
        RequestValidationError,
        ValidationError,
        SettingsError,
        ChatAiError,
        ModelNotAvailableError,
        AiError,
        RuntimeRefreshInProgressError,
        WorkflowStoreError,
        WorkflowValidationError,
    ):
        app.add_exception_handler(exception_type, error_response)
