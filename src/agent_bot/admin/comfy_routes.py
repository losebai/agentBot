"""Comfy 插件只接受本机同源管理页面发送的无参数启动意图。"""

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse


def install_comfy_routes(router: APIRouter, runtime) -> None:
    """继承统一管理认证，不允许浏览器指定命令、路径或启动参数。"""

    def reject(code: str, message: str, status: int) -> JSONResponse:
        """公开稳定错误，避免操作系统及路径异常进入管理响应。"""
        return JSONResponse({"code": code, "message": message}, status_code=status)

    @router.get("/comfy/status", response_model=None)
    async def status() -> dict | JSONResponse:
        """状态读取不会启动服务，插件停用时不分配连接或进程。"""
        launcher = getattr(runtime, "comfy_launcher", None)
        if launcher is None:
            return reject("COMFY_PLUGIN_DISABLED", "请先启用 Comfy 插件并重启后端。", 409)
        return await launcher.snapshot(runtime.config.chat_local_workflow_base_url)

    @router.post("/comfy/start", response_model=None)
    async def start(request: Request) -> dict | JSONResponse:
        """回环、同源与空请求体共同约束启动入口，执行使用已生效静态配置。"""
        if (request.client is None or request.client.host not in {"127.0.0.1", "::1"}
                or request.url.hostname not in {"127.0.0.1", "localhost", "::1"}
                or request.headers.get("origin") != f"{request.url.scheme}://{request.url.netloc}"):
            return reject("COMFY_LOCAL_ONLY", "请在本机管理页面启动 ComfyUI。", 403)
        body = await request.body()
        if request.query_params or body not in (b"", b"{}"):
            return reject("COMFY_NO_ARGUMENTS", "启动请求不能指定命令或参数。", 422)
        launcher = getattr(runtime, "comfy_launcher", None)
        if launcher is None:
            return reject("COMFY_PLUGIN_DISABLED", "请先启用 Comfy 插件并重启后端。", 409)
        return await launcher.start(runtime.config.chat_local_workflow_base_url)
