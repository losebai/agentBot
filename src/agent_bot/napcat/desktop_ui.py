"""QQ 插件请求的原生登录窗口，凭据不进入网页。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent_bot.napcat.configuration import read_object
from agent_bot.napcat.contracts import NapCatErrorCode, NapCatMode, NapCatState


class NapCatDesktopState(BaseModel):
    """私有通知只接受状态与活动端口，不接受任意 URL 或秘密。"""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    mode: NapCatMode = "external"
    state: NapCatState = "not_started"
    webui_ready: bool = False
    webui_port: int = Field(default=6099, ge=1, le=65535)
    error_code: NapCatErrorCode | None = None

    def login_url(self) -> str:
        """地址仅由本次活动端口和固定路径生成，不读取待重启草稿。"""
        return f"http://127.0.0.1:{self.webui_port}/webui/"



def open_login(
    window: Any, directory: Path, current: Callable[[], NapCatDesktopState],
) -> Any:
    """只打开所属且仍存活的登录页，网页无法传入 URL 或读取登录密钥。"""
    import webview

    from agent_bot.desktop_settings import show_error

    state = current()
    if state.mode != "managed" or not state.webui_ready:
        show_error(window, "所属 NapCat 登录页尚未就绪，请查看 QQ 插件状态。")
        return None
    try:
        data = read_object(directory / "data/napcat/config/webui.json")
        token = data.get("token")
        if not isinstance(token, str) or not token:
            raise ValueError("Missing WebUI token")
    except (OSError, ValueError):
        show_error(window, "NapCat 登录配置无法读取，请检查配置文件。")
        return None
    show_error(window, f"NapCat WebUI 登录密钥：\n{token}\n\n请在接下来打开的登录页输入。")
    # 原生提示期间可能重启或退出后端，不能再打开已经失效或被接管的旧端口。
    active = current()
    if not active.webui_ready or active.webui_port != state.webui_port:
        return None
    return webview.create_window("NapCat QQ 登录", url=state.login_url(), width=1000, height=760)
