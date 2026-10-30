"""仅向所属回环 WebUI 获取登录二维码，管理凭据始终留在本机后端。"""

import hashlib
import threading
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from agent_bot.napcat.configuration import read_object
from agent_bot.napcat.contracts import NapCatLogin


class WebUIError(ValueError):
    """固定错误消息，不公开第三方响应、密钥或二维码原文。"""


class NapCatWebUI:
    """缓存本次所属进程的短期凭据，避免二维码轮询触发登录限流。"""

    def __init__(self, directory: Path, port: int, owned: Callable[[], bool]) -> None:
        """构造不访问磁盘或网络，归属检查由进程持有者提供。"""
        self.directory, self.port, self.owned = directory, port, owned
        self._lock = threading.Lock()
        self._token = ""
        self._credential = ""
        self._expires = 0.0

    def _post(self, client: httpx.Client, path: str, body: dict) -> dict:
        """发送固定接口前后确认归属，不向重定向地址转交凭据。"""
        if not self.owned():
            raise WebUIError("所属 NapCat 登录服务已停止。")
        response = client.post(path, json=body)
        response.raise_for_status()
        value = response.json()
        if not self.owned():
            raise WebUIError("所属 NapCat 登录服务已停止。")
        if (not isinstance(value, dict) or value.get("code") != 0
                or not isinstance(value.get("data"), dict)):
            self._credential = ""
            raise WebUIError("NapCat 登录接口暂不可用，请刷新或打开后台管理界面。")
        return value["data"]

    def login(self, *, refresh: bool = False) -> NapCatLogin:
        """获取当前扫码状态；只在用户请求刷新时更换二维码，不触发 QQ 登录。"""
        with self._lock:
            try:
                if not self.owned():
                    raise WebUIError("所属 NapCat 登录服务尚未就绪。")
                token = read_object(self.directory / "data/napcat/config/webui.json").get("token")
                if not isinstance(token, str) or not token:
                    raise WebUIError("NapCat 后台登录配置无法读取。")
                with httpx.Client(base_url=f"http://127.0.0.1:{self.port}", trust_env=False,
                                  timeout=15 if refresh else 3, follow_redirects=False) as client:
                    # 上游凭据有效期一小时，提前续期；失败不切换协议或绕过二次验证。
                    if (not self._credential or token != self._token
                            or time.monotonic() >= self._expires):
                        auth = self._post(client, "/api/auth/login", {
                            "hash": hashlib.sha256((token + ".napcat").encode()).hexdigest(),
                        })
                        if auth.get("require2FA"):
                            raise WebUIError("请先在后台管理界面完成 NapCat 二次验证。")
                        credential = auth.get("Credential")
                        if not isinstance(credential, str) or not credential:
                            raise WebUIError("NapCat 后台认证失败。")
                        self._token, self._credential = token, credential
                        self._expires = time.monotonic() + 3000
                    client.headers["Authorization"] = f"Bearer {self._credential}"
                    data = self._post(client, "/api/QQLogin/CheckLoginStatus", {})
                    if refresh and not data.get("isLogin"):
                        self._post(client, "/api/QQLogin/RefreshQRcode", {})
                        data = self._post(client, "/api/QQLogin/CheckLoginStatus", {})
                if data.get("isLogin") is True and data.get("coreReady") is True:
                    return NapCatLogin("online")
                phase = data.get("loginPhase")
                if (data.get("qrLoginAccepted") is True
                        or phase in {"qrcode_scanned", "initializing"}):
                    return NapCatLogin("scanned")
                if data.get("loginError"):
                    return NapCatLogin("error")
                if phase == "reconnecting":
                    return NapCatLogin("connecting")
                qr = data.get("qrcodeurl")
                if not qr:
                    return NapCatLogin("generating")
                parsed = urlsplit(qr)
                if (not isinstance(qr, str) or parsed.scheme != "https"
                        or parsed.hostname != "txz.qq.com" or parsed.username):
                    raise WebUIError("NapCat 返回的登录二维码无效。")
                return NapCatLogin("waiting", qr)
            except WebUIError:
                raise
            except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError) as exc:
                raise WebUIError("NapCat 登录信息读取失败，请刷新或打开后台管理界面。") from exc
