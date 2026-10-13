"""Serve the built management page without a catch-all over robot API routes."""

from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse, JSONResponse
from starlette.staticfiles import StaticFiles


def install_static(app: FastAPI, directory: Path | None = None) -> None:
    """Install only the UI root and asset directory; missing paths remain real 404s."""
    root = directory or Path(__file__).parent / "static"
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
