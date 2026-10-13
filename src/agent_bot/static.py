"""使用发布资源路径提供管理页面，不覆盖机器人 API 路由。"""

from importlib.metadata import distribution
from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles


def install_static(app: FastAPI, directory: Path | None = None) -> None:
    """开发源码与 wheel 共用资源定位，缺失构建时返回明确错误。"""
    root = directory or Path(distribution('agent-bot').locate_file('agent_bot/static'))
    if (root / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=root / "assets"), name="management-assets")

    @app.get("/", include_in_schema=False)
    async def index():
        """Explain a missing frontend build without disabling the management API."""
        if not (root / "index.html").is_file():
            return JSONResponse(
                {"code": "UI_NOT_BUILT", "message": "请先在 web 目录构建管理界面"}, status_code=503
            )
        return FileResponse(root / "index.html", headers={"Cache-Control": "no-cache"})
