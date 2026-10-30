"""区分公开观察与含本机路径的内部启动参数，避免秘密进入管理响应。"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

NapCatMode = Literal["external", "managed"]
NapCatState = Literal["not_started", "starting", "running", "stopped", "failed", "plugin_disabled"]
NapCatErrorCode = Literal[
    "PAYLOAD_INVALID", "CONFIG_INVALID", "CONFIG_CONFLICT", "INSTANCE_BUSY", "PORT_UNAVAILABLE",
    "START_FAILED", "PROCESS_EXITED", "WEBUI_UNAVAILABLE", "PLATFORM_UNSUPPORTED",
    "AUTO_LOGIN_FAILED",
]


@dataclass(frozen=True)
class NapCatStatus:
    """仅公开有限状态和固定错误码，错误原文留在调用边界之外。"""

    mode: NapCatMode = "external"
    state: NapCatState = "not_started"
    webui_ready: bool = False
    error_code: NapCatErrorCode | None = None


@dataclass(frozen=True)
class NapCatPluginStatus:
    """插件卡片接收能力、状态和活动管理端口，不接触命令或密钥。"""

    available: bool
    plugin_enabled: bool
    status: NapCatStatus
    webui_port: int | None = None


@dataclass(frozen=True)
class NapCatLogin:
    """仅返回扫码所需信息，不暴露 WebUI 密钥或短期管理凭据。"""

    status: Literal["generating", "waiting", "scanned", "connecting", "online", "error"]
    qr_code_url: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class NapCatLaunch:
    """已校验的本地命令不参与 HTTP 响应或日志。"""

    command: tuple[str, ...] = field(repr=False)
    cwd: Path = field(repr=False)
    environment: dict[str, str] = field(repr=False)
    webui_port: int = 6099
