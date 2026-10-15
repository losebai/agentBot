"""QQ 插件操作所属桌面 NapCat；不接受命令、路径、端口或外部实例参数。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse

from agent_bot.napcat.contracts import NapCatPluginStatus, NapCatStatus

if TYPE_CHECKING:
    from agent_bot.napcat.supervisor import NapCatSupervisor


def install_napcat_routes(router: APIRouter, provider: Callable[[], NapCatStatus]) -> None:
    """复用管理认证；只有本机同源页面可启动所属实例或通知桌面打开登录。"""

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
        return NapCatPluginStatus(
            available=supervisor is not None and bool(
                getattr(request.app.state, "napcat_backend_url", None)),
            plugin_enabled=supervisor is not None and "qqbot" in (
                supervisor.config.app_enabled_plugins),
            status=provider(),
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

    @router.post("/napcat/login", response_model=None)
    async def login(request: Request) -> dict[str, bool] | JSONResponse:
        """WebUI 凭据仍留在本机原生窗口，网页仅发送无参数的登录意图。"""
        supervisor = context(request)
        if isinstance(supervisor, JSONResponse):
            return supervisor
        notify = getattr(request.app.state, "napcat_open_login", None)
        if not supervisor.snapshot().webui_ready or notify is None:
            return reject("NAPCAT_LOGIN_UNAVAILABLE", "NapCat 登录页尚未就绪。", 409)
        notify()
        return {"accepted": True}
