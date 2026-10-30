"""仅桌面入口启动的可取消托管生命周期，失败不阻断管理后端。"""

import sys
import threading
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path

import httpx

from agent_bot.config import RuntimeConfig
from agent_bot.napcat.configuration import ConfigurationError, configure_instance
from agent_bot.napcat.contracts import NapCatErrorCode, NapCatLogin, NapCatStatus
from agent_bot.napcat.payload import PayloadError, is_link, load_payload
from agent_bot.napcat.webui import NapCatWebUI, WebUIError


class NapCatSupervisor:
    """以活动配置观察所属 NapCat，不接管外部实例或读取 QQ 登录资料。"""

    def __init__(self, directory: Path, config: RuntimeConfig) -> None:
        """构造不创建文件、不启动线程，配置只使用本次后端快照。"""
        self.directory, self.config = directory, config
        self._status = NapCatStatus(mode=config.agent_bot_napcat_mode)
        self._lock = threading.Lock()
        self._operation = threading.Lock()
        self._cancelled = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_status: Callable[[NapCatStatus], None] | None = None
        self._webui: NapCatWebUI | None = None

    def snapshot(self) -> NapCatStatus:
        """返回不可变的非秘密状态，不暴露启动参数或配置原文。"""
        with self._lock:
            return self._status

    def login(self, *, refresh: bool = False) -> NapCatLogin:
        """访问当前所属实例并记住缺省账号，停机后丢弃旧二维码结果。"""
        with self._lock:
            client = self._webui
        if client is None or not self.snapshot().webui_ready:
            raise WebUIError("NapCat 登录服务尚未就绪。")
        result = client.login(refresh=refresh)
        with self._lock:
            if client is not self._webui or not self._status.webui_ready:
                raise WebUIError("所属 NapCat 登录服务已停止。")
        return result

    def _publish(self, state: str, error: NapCatErrorCode | None = None, ready=False) -> None:
        """固定枚举事件同时用于原生通知和无秘密日志。"""
        status = NapCatStatus(self.snapshot().mode, state, ready, error)
        with self._lock:
            self._status = status
        folder = self.directory / "logs"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "napcat.log").open("a", encoding="utf-8") as log:
                log.write(f"{state} {error or ''}\n")
        except OSError:
            # 状态仍从内存可见；日志故障不覆盖实际托管错误或阻断资源释放。
            pass
        if self.on_status is not None:
            self.on_status(status)

    def start(self, backend_url: str, *, managed: bool = False) -> None:
        """插件显式启动只改变当前实例；重复点击合并，失败后允许手动重试。"""
        with self._operation:
            if self._cancelled.is_set() or (self._thread is not None and self._thread.is_alive()):
                return
            if managed:
                with self._lock:
                    self._status = NapCatStatus(mode="managed")
            if self.snapshot().mode == "external":
                return
            if "qqbot" not in self.config.app_enabled_plugins:
                self._publish("plugin_disabled")
                return
            self._publish("starting")
            self._thread = threading.Thread(target=self._run, args=(backend_url,), daemon=True)
            self._thread.start()

    def _run(self, backend_url: str) -> None:
        """显式启动配套 Node；工作线程唯一持有 Job 和锁并统一清理。"""
        from agent_bot.desktop_settings import check_port_available
        from agent_bot.napcat.windows_process import InstanceLease, WindowsJob

        stage: NapCatErrorCode = "PAYLOAD_INVALID"
        try:
            payload = load_payload(self.directory / "runtime/napcat-node", runtime=True)
            if sys.platform != "win32":
                self._publish("failed", "PLATFORM_UNSUPPORTED")
                return
            if self.config.onebot_v11_ws_urls or self.config.onebot_v11_api_roots:
                self._publish("failed", "CONFIG_CONFLICT")
                return
            stage = "INSTANCE_BUSY"
            data = self.directory / "data"
            if is_link(data):
                raise OSError("Invalid instance directory")
            with ExitStack() as resources:
                resources.enter_context(InstanceLease(data / "napcat"))
                stage = "PORT_UNAVAILABLE"
                check_port_available("127.0.0.1", self.config.agent_bot_napcat_webui_port)
                stage = "CONFIG_INVALID"
                launch = configure_instance(self.directory, payload, self.config, backend_url)
                if self._cancelled.is_set():
                    return
                stage = "START_FAILED"
                job = WindowsJob()
                # LIFO 保证关闭所属 Job 后才释放目录锁，第二窗口不能提前接管。
                resources.callback(job.close)
                process = job.spawn(launch)
                resources.callback(process.close)
                with self._lock:
                    self._webui = NapCatWebUI(self.directory, launch.webui_port, lambda: (
                        not self._cancelled.is_set()
                        and job.owns_listener("127.0.0.1", launch.webui_port)))
                deadline = time.monotonic() + 30
                ready = False
                auto_login_failed = False
                next_login_check = time.monotonic() + 2
                with httpx.Client(trust_env=False, timeout=0.5) as client:
                    while not self._cancelled.wait(0.2):
                        if job.active_processes() == 0:
                            self._publish("failed", "PROCESS_EXITED")
                            return
                        owns = job.owns_listener("127.0.0.1", launch.webui_port)
                        if not ready and owns:
                            try:
                                response = client.get(f"http://127.0.0.1:{launch.webui_port}/webui/")
                            except httpx.HTTPError:
                                response = None
                            # HTTP 故障也必须走下面的截止时间检查，不能无限等待。
                            if response is not None and response.status_code == 200:
                                ready = True
                                self._publish("running", ready=True)
                        if ready and not owns:
                            self._publish("failed", "WEBUI_UNAVAILABLE")
                            return
                        if not ready and time.monotonic() >= deadline:
                            self._publish("failed", "WEBUI_UNAVAILABLE")
                            return
                        if (ready and self._webui is not None
                                and self._webui.auto_login_configured
                                and self.snapshot().error_code == "AUTO_LOGIN_FAILED"):
                            self._publish("running", ready=True)
                        # 与管理页面是否打开无关；只观察至保存成功，失败则等待用户显式重试。
                        if (ready and not auto_login_failed and self._webui is not None
                                and not self._webui.auto_login_configured
                                and time.monotonic() >= next_login_check):
                            next_login_check = time.monotonic() + 2
                            try:
                                self.login()
                            except WebUIError:
                                auto_login_failed = True
                                if not self._cancelled.is_set():
                                    self._publish("running", "AUTO_LOGIN_FAILED", ready=True)
        except PayloadError:
            self._publish("failed", "PAYLOAD_INVALID")
        except ConfigurationError:
            self._publish("failed", "CONFIG_INVALID")
        except Exception:
            # 上游/OS 异常可能含路径或凭据，只公开所在阶段的固定错误码。
            self._publish("failed", stage)
        finally:
            with self._lock:
                self._webui = None
            if self._cancelled.is_set() and self.snapshot().state != "failed":
                self._publish("stopped")

    def stop(self) -> None:
        """取消准备和观察，等待工作线程在统一出口释放进程树及锁。"""
        with self._operation:
            self._cancelled.set()
            if self._thread is not None:
                self._thread.join(timeout=35)
                if self._thread.is_alive():
                    raise RuntimeError("NapCat 停止超时。")
