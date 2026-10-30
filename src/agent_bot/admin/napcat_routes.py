"""QQ 插件操作所属桌面 NapCat；不接受命令、路径、端口或外部实例参数。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from agent_bot.napcat.contracts import NapCatPluginStatus, NapCatStatus
from agent_bot.napcat.webui import WebUIError

if TYPE_CHECKING:
    from agent_bot.napcat.supervisor import NapCatSupervisor


def install_napcat_routes(router: APIRouter, provider: Callable[[], NapCatStatus]) -> None:
    """复用管理认证；本机同源页面可扫码或通知桌面用浏览器打开所属后台。"""

    def reject(code: str, message: str, status: int) -> JSONResponse:
        """错误使用固定文案，不回传 OS、配置或第三方异常正文。"""
        return JSONResponse({"code": code, "message": message}, status_code=status)

    def context(request: Request) -> NapCatSupervisor | JSONResponse:
        """写操作要求回环来源和同源 Origin，免密码桌面也不能被跨站页面控制。"""
        if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
                or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}"):
            return reject("NAPCAT_LOCAL_ONLY", "请在本机管理页面操作 QQ 插件。", 403)
        supervisor = getattr(request.app.state, "napcat", None)
        if supervisor is None or not getattr(request.app.state, "napcat_backend_url", None):
            return reject("NAPCAT_DESKTOP_REQUIRED", "此入口需要 Windows 桌面版。", 409)
        if "qqbot" not in supervisor.config.app_enabled_plugins:
            return reject("NAPCAT_PLUGIN_DISABLED", "请先启用 QQ 插件并重启后端。", 409)
        return supervisor

    @router.get("/napcat/status", response_model=NapCatStatus)
    async def napcat_status() -> NapCatStatus:
        """读取进程内存，不因页面查询启动或探测外部程序。"""
        return provider()

    @router.get("/napcat/plugin")
    async def plugin_state(request: Request) -> NapCatPluginStatus:
        """能力信息只含布尔值及公开状态，不提供 WebUI 密钥或地址。"""
        supervisor = getattr(request.app.state, "napcat", None)
        status = provider()
        return NapCatPluginStatus(
            available=supervisor is not None and bool(
                getattr(request.app.state, "napcat_backend_url", None)),
            plugin_enabled=supervisor is not None and "qqbot" in (
                supervisor.config.app_enabled_plugins),
            status=status,
            webui_port=(supervisor.config.agent_bot_napcat_webui_port
                        if supervisor is not None and status.webui_ready else None),
        )

    @router.post("/napcat/start", response_model=NapCatStatus)
    async def start(request: Request) -> NapCatStatus | JSONResponse:
        """仅使用后端实际监听地址，启动工作线程而不阻塞管理请求。"""
        supervisor = context(request)
        if isinstance(supervisor, JSONResponse):
            return supervisor
        await asyncio.to_thread(
            supervisor.start, request.app.state.napcat_backend_url, managed=True,
        )
        return supervisor.snapshot()

    async def login_result(request: Request, *, refresh: bool) -> JSONResponse:
        """同源授权页面才能读取临时扫码数据，不接受网页指定上游地址。"""
        from dataclasses import asdict

        supervisor = context(request)
        if isinstance(supervisor, JSONResponse):
            return supervisor
        try:
            result = await asyncio.to_thread(supervisor.login, refresh=refresh)
        except WebUIError as exc:
            response = reject("NAPCAT_LOGIN_UNAVAILABLE", str(exc), 409)
            response.headers["Cache-Control"] = "no-store"
            return response
        return JSONResponse(asdict(result), headers={"Cache-Control": "no-store"})

    @router.post("/napcat/login")
    async def login(request: Request) -> JSONResponse:
        """读取所属实例的扫码状态，不启动 QQ 或更换已有二维码。"""
        return await login_result(request, refresh=False)

    @router.post("/napcat/login/refresh")
    async def refresh_login(request: Request) -> JSONResponse:
        """用户显式刷新二维码，旧二维码由上游作废。"""
        return await login_result(request, refresh=True)

    @router.post("/napcat/admin", response_model=None)
    async def open_admin(request: Request) -> dict[str, bool] | JSONResponse:
        """网页只发送无参数意图，本机桌面构造地址并交给默认浏览器。"""
        supervisor = context(request)
        if isinstance(supervisor, JSONResponse):
            return supervisor
        notify = getattr(request.app.state, "napcat_open_login", None)
        if not supervisor.snapshot().webui_ready or notify is None:
            return reject("NAPCAT_LOGIN_UNAVAILABLE", "NapCat 登录页尚未就绪。", 409)
        notify()
        return {"accepted": True}
