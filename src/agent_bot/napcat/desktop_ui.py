"""QQ 插件请求默认浏览器打开所属后台，地址不由页面提供。"""

from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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
) -> None:
    """按用户授权将密钥编码到所属后台 URL，交给本机默认浏览器。"""
    import webbrowser

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
    # 配置读取期间可能重启或退出后端，不能打开被接管的旧端口。
    active = current()
    if not active.webui_ready or active.webui_port != state.webui_port:
        return None
    # 直接进入官方登录路由，避免根路由解码后再次拼接而损坏含 &/+ 的密钥。
    url = state.login_url() + "web_login?" + urlencode({"token": token})
    if not webbrowser.open(url, new=2):
        show_error(window, "默认浏览器无法打开，请检查系统浏览器设置。")
