"""Windows 桌面壳及其专属后端进程；复用管理界面和正常应用生命周期。"""

from __future__ import annotations

import asyncio
import base64
import html
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from agent_bot.napcat.desktop_ui import NapCatDesktopState

_READY = "AGENT_BOT_DESKTOP_READY "
_STAGE = "AGENT_BOT_DESKTOP_STAGE "
_ERROR = "AGENT_BOT_DESKTOP_ERROR "
_NAPCAT = "AGENT_BOT_NAPCAT_STATE "
_NAPCAT_LOGIN = "AGENT_BOT_NAPCAT_OPEN_LOGIN"


def prepare_directory() -> Path:
    """冻结程序以 EXE 所在目录读取配置和保存数据，源码入口保留当前目录。"""
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
    return Path.cwd()


class BackendProcess:
    """通过私有标准输入管道管理本窗口启动的后端，不接管其他服务。"""

    def __init__(
        self, command: list[str], directory: Path, log_path: Path, *,
        desktop_handshake: bool = False,
    ) -> None:
        """登记启动路径；构造时不启动服务、不创建业务数据。"""
        self.command = command
        self.directory = directory
        self.log_path = log_path
        from agent_bot.napcat.desktop_ui import NapCatDesktopState

        self.napcat_state = NapCatDesktopState()
        self.on_napcat_login: Callable[[], None] | None = None
        self.desktop_handshake = desktop_handshake
        self.job = None
        self.process: subprocess.Popen[str] | None = None
        self.url: str | None = None
        self.stage = "加载后端组件"
        self.error: str | None = None
        self._started_at = 0.0
        self._ready = threading.Event()
        self.finished = threading.Event()

    def start(self) -> None:
        """生产后端先加入所属 Job 再握手，普通测试子进程保留直接启动。"""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._started_at = time.monotonic()
        self.process = subprocess.Popen(
            self.command, cwd=self.directory,
            env=os.environ | {
                "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
                "PYINSTALLER_RESET_ENVIRONMENT": "1",
            },
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if self.desktop_handshake:
            from agent_bot.napcat.windows_process import WindowsJob

            try:
                self.job = WindowsJob()
                self.job.assign(self.process.pid)
                self.process.stdin.write("G")
                self.process.stdin.flush()
            except Exception:
                self.process.terminate()
                self.process.wait(timeout=5)
                if self.job is not None:
                    self.job.close()
                raise RuntimeError("后端进程隔离失败，已停止启动。") from None
        threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self) -> None:
        """就绪只接受所属子进程的通知，避免端口冲突时误开其他实例。"""
        assert self.process is not None and self.process.stdout is not None
        try:
            with self.log_path.open("w", encoding="utf-8") as log, self.process.stdout:
                for line in self.process.stdout:
                    if line.strip() == _NAPCAT_LOGIN:
                        if self.on_napcat_login is not None and self.napcat_snapshot().webui_ready:
                            # 原生提示等待用户期间仍须持续读取后端状态及退出通知。
                            threading.Thread(target=self.on_napcat_login, daemon=True).start()
                        continue
                    if line.startswith(_NAPCAT):
                        from pydantic import ValidationError

                        from agent_bot.napcat.desktop_ui import NapCatDesktopState

                        try:
                            self.napcat_state = NapCatDesktopState.model_validate_json(
                                line.removeprefix(_NAPCAT))
                        except ValidationError:
                            # 畸形私有通知不落盘，避免未知字段把秘密带入日志。
                            pass
                        continue
                    log.write(line)
                    log.flush()
                    if line.startswith(_READY):
                        self.url = line.removeprefix(_READY).strip()
                        self._ready.set()
                    elif line.startswith(_STAGE):
                        self.stage = json.loads(line.removeprefix(_STAGE))
                    elif line.startswith(_ERROR):
                        # 仅接受后端专用通道中已脱敏的错误，不把日志或 traceback 展示到页面。
                        self.error = json.loads(line.removeprefix(_ERROR))
        finally:
            self.process.wait()
            if self.job is not None:
                self.job.close()
            self.napcat_state = self.napcat_snapshot()
            self.finished.set()
            self._ready.set()

    def napcat_snapshot(self) -> NapCatDesktopState:
        """后端退出立即撤销登录就绪，覆盖输出线程尚未处理退出通知的间隙。"""
        state = self.napcat_state
        if self.process is None or self.process.poll() is not None:
            return state.model_copy(update={"state": "stopped", "webui_ready": False})
        return state

    def wait_ready(
        self, timeout: float = 60, *, on_status: Callable[[str], None] | None = None,
        cancelled: threading.Event | None = None,
    ) -> str:
        """从进程启动时计时并同步阶段，配置失败不再只给出泛化的等待提示。"""
        previous = None
        while True:
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError("后端启动已取消。")
            if on_status is not None and self.stage != previous:
                previous = self.stage
                on_status(self.stage)
            if self._ready.is_set():
                break
            remaining = timeout - (time.monotonic() - self._started_at)
            if remaining <= 0:
                raise RuntimeError(f"后端启动超时，停在：{self.stage}。请检查配置与日志。")
            self._ready.wait(min(remaining, 0.2))
        if self.finished.is_set() or self.url is None:
            raise RuntimeError(
                f"后端启动失败：{self.error}"
                if self.error else "后端启动失败，请检查配置、数据库初始化和端口占用。"
            )
        return self.url

    def stop(self) -> None:
        """先关闭管道触发正常停机，卡住时只终止本窗口持有的子进程。"""
        if self.process is None:
            return
        if self.process.stdin is not None:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=5)
        self.finished.wait(timeout=2)


class DesktopController:
    """串行管理本窗口所属的后端，旧进程退出通知不能覆盖新实例的界面。"""

    def __init__(
        self, directory: Path, command: list[str], show_status: Callable[[str], None],
        open_url: Callable[[str], None],
    ) -> None:
        """注入窗口操作，进程管理不依赖原生 GUI，便于验证实际子进程生命周期。"""
        self.directory, self.command = directory, command
        self.show_status, self.open_url = show_status, open_url
        self.closed = threading.Event()
        self.backend: BackendProcess | None = None
        self.on_napcat_login: Callable[[BackendProcess], None] | None = None
        self._operation = threading.Lock()

    def restart(self, port: int | None = None) -> None:
        """先验证和保存新端口，再停止旧后端，整个交接只允许一个操作进行。"""
        from agent_bot.desktop_settings import check_port_available, connection_settings, save_port

        if not self._operation.acquire(blocking=False):
            raise ValueError("后端正在启动或重启，请稍后再试。")
        try:
            if self.closed.is_set():
                return
            if port is not None:
                host, port = connection_settings(self.directory, port_override=port)
                current = self.backend
                owns_port = (
                    current is not None and current.url is not None
                    and current.process is not None and current.process.poll() is None
                    and urlsplit(current.url).port == port
                )
                if not owns_port:
                    check_port_available(host, port)
                save_port(self.directory, port)
            self.show_status("正在重启后端…" if self.backend else "正在启动服务…")
            if self.backend is not None:
                self.backend.stop()
            if self.closed.is_set():
                return
            backend = BackendProcess(
                self.command, self.directory, self.directory / "logs" / "desktop-backend.log",
                desktop_handshake=sys.platform == "win32" and "--desktop-child" in self.command,
            )
            self.backend = backend
            if self.on_napcat_login is not None:
                backend.on_napcat_login = lambda: self.on_napcat_login(backend)
            try:
                backend.start()
                url = backend.wait_ready(
                    on_status=lambda stage: self.show_status(f"正在启动服务…\n{stage}"),
                    cancelled=self.closed,
                )
                if not self.closed.is_set():
                    self.open_url(url)
                    threading.Thread(target=self._watch, args=(backend,), daemon=True).start()
            except (OSError, RuntimeError) as exc:
                backend.stop()
                if not self.closed.is_set():
                    message = str(exc) if isinstance(exc, RuntimeError) else "无法启动后端进程。"
                    self.show_status(
                        f"{message}\n可通过“服务”菜单修改端口或重启后端。\n"
                        "详情：logs/desktop-backend.log",
                    )
        finally:
            self._operation.release()

    def _watch(self, backend: BackendProcess) -> None:
        """仅当前进程的意外退出可改变界面，正常重启时忽略旧实例结束通知。"""
        backend.finished.wait()
        with self._operation:
            if not self.closed.is_set() and self.backend is backend:
                self.show_status("后端已停止，可通过“服务”菜单重新启动。\n详情：logs/desktop-backend.log")

    def stop(self) -> None:
        """先取消等待再串行停止进程，避免关窗与重启竞争留下后台服务。"""
        self.closed.set()
        with self._operation:
            if self.backend is not None:
                self.backend.stop()


def serve(app: Any, host: str, port: int, *, napcat: Any = None) -> None:
    """监听后向 QQ 插件开放所属托管入口，停机先释放 QQ 再关闭业务资源。"""
    import uvicorn

    class DesktopServer(uvicorn.Server):
        """监听成功后通知桌面进程，复用 uvicorn 的启动与退出实现。"""

        async def startup(self, sockets: list[socket.socket] | None = None) -> None:
            """应用 lifespan 与监听全部完成后才允许窗口加载前端。"""
            await super().startup(sockets=sockets)
            if self.started:
                address = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
                if ":" in address:
                    address = f"[{address}]"
                url = f"http://{address}:{port}/"
                print(f"{_READY}{url}", flush=True)
                if napcat is not None:
                    app.state.napcat_backend_url = url

        async def shutdown(self, sockets=None) -> None:
            """先停止第三方进程，防止业务资源释放后继续接收消息。"""
            if napcat is not None:
                await asyncio.to_thread(napcat.stop)
            await super().shutdown(sockets=sockets)

    server = DesktopServer(uvicorn.Config(app, host=host, port=port, log_level="info"))

    def watch_parent() -> None:
        """EOF 同时覆盖正常关窗和桌面进程异常退出，不开放额外控制端口。"""
        sys.stdin.read(1)
        server.should_exit = True

    threading.Thread(target=watch_parent, daemon=True).start()
    server.run()


def run_backend() -> int:
    """全新安装先生成本地默认配置；已有部署读取数据库中的权威运行值。"""
    # 冻结解释器可能忽略 PYTHONIOENCODING，明确匹配父进程的 UTF-8 日志解码。
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    # 所属父进程完成 Job 归属后才允许初始化和派生第三方进程。
    if sys.platform == "win32" and sys.stdin.read(1) != "G":
        return 1
    print(_STAGE + json.dumps("加载后端组件"), flush=True)
    from agent_bot.admin.config import AdminConfig
    from agent_bot.bootstrap import load_startup_settings
    from agent_bot.cli_errors import describe_config_error
    from agent_bot.first_run import FirstRunError, prepare_first_run
    from agent_bot.settings import SettingsError

    try:
        print(_STAGE + json.dumps("检查首次启动配置"), flush=True)
        enable_managed = False
        # 仅全新桌面安装自动选择托管，已有部署不因添加运行资源而改变连接。
        if (not any(Path(name).exists() for name in (".env", ".env.prod"))
                and Path("runtime/napcat").exists()):
            # 载荷校验属于托管启动阶段；损坏资源不能阻止管理后端提供诊断。
            enable_managed = True
        prepare_first_run(Path.cwd(), enable_managed_napcat=enable_managed)
        print(_STAGE + json.dumps("读取启动配置和数据库"), flush=True)
        admin = AdminConfig.from_environment()
        stored = load_startup_settings()
    except FirstRunError as exc:
        print(_ERROR + json.dumps(str(exc)), flush=True)
        return 1
    except (ValueError, SettingsError) as exc:
        print(_ERROR + json.dumps(describe_config_error(exc)), flush=True)
        return 1
    print(_STAGE + json.dumps("装配机器人和管理接口"), flush=True)
    from dataclasses import asdict

    from agent_bot.application import create_application
    from agent_bot.napcat.supervisor import NapCatSupervisor

    napcat = NapCatSupervisor(Path.cwd(), stored.config)

    def notify_napcat(status) -> None:
        """私有通知只包含经过固定契约约束的状态与本次活动端口。"""
        print(_NAPCAT + json.dumps(asdict(status) | {
            "webui_port": stored.config.agent_bot_napcat_webui_port}), flush=True)

    napcat.on_status = notify_napcat
    app = create_application(stored, admin, napcat=napcat)

    def request_login() -> None:
        """插件只能发送固定登录意图，具体窗口和密钥由所属父进程处理。"""
        print(_NAPCAT_LOGIN, flush=True)

    app.state.napcat_open_login = request_login
    print(_STAGE + json.dumps("准备数据库和队列"), flush=True)
    serve(app, str(stored.config.host), stored.config.port, napcat=napcat)
    return 0


@lru_cache(maxsize=1)
def status_icon() -> str:
    """内嵌随程序分发的图标，后端未启动时也可显示且无需重复读取。"""
    icon = Path(__file__).with_name("desktop_icon.ico")
    if not icon.is_file():
        # 源码开发直接复用图标资产；wheel 与 EXE 由打包规则放入包目录。
        icon = Path(__file__).resolve().parents[2] / "assets/desktop/agentbot-icon-v2.ico"
    return "data:image/x-icon;base64," + base64.b64encode(icon.read_bytes()).decode("ascii")


def status_page(message: str) -> str:
    """图标与状态文字整体居中；脱敏错误转义后显示，后端未启动也可独立渲染。"""
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>AgentBot</title><style>'
        '*{box-sizing:border-box}body{font:16px system-ui;margin:0;color:#303030;'
        'background:#fafafa;min-height:100vh;display:grid;place-items:center;padding:32px}'
        'main{display:flex;flex-direction:column;align-items:center;text-align:center;'
        'max-width:560px;width:100%;gap:20px}'
        'img{width:96px;height:96px;object-fit:contain}'
        'p{margin:0;line-height:1.8;white-space:pre-wrap;overflow-wrap:anywhere}</style>'
        f'<body><main><img src="{status_icon()}" alt="AgentBot">'
        f'<p role="status" aria-live="polite">{html.escape(message)}</p></main></body></html>'
    )


def main() -> int:
    """桌面仅保留服务菜单；QQ 插件通过私有通知请求所属登录窗口。"""
    import webview
    from webview.menu import Menu, MenuAction

    from agent_bot.desktop_settings import (
        connection_settings,
        port_dialog,
        show_error,
    )

    directory = prepare_directory()
    command = (
        [str(directory / "agent-bot.exe"), "--desktop-child"]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "agent_bot.desktop", "--desktop-child"]
    )
    window = webview.create_window(
        "AgentBot", html=status_page("正在启动服务…"), width=1280, height=860,
        min_size=(800, 600), text_select=True,
    )

    def show_status(message: str) -> None:
        """显示已脱敏状态，关闭窗口后不再操作 WebView。"""
        if not controller.closed.is_set():
            window.load_html(status_page(message))

    controller = DesktopController(directory, command, show_status, window.load_url)
    login_windows: list = []

    def open_plugin_login(backend: BackendProcess) -> None:
        """忽略旧后端通知，关窗或重启期间禁止打开失去归属的登录窗口。"""
        from agent_bot.napcat.desktop_ui import NapCatDesktopState, open_login

        def current() -> NapCatDesktopState:
            """每次读取同时确认所属实例和窗口仍有效。"""
            if controller.closed.is_set() or controller.backend is not backend:
                return NapCatDesktopState()
            return backend.napcat_snapshot()

        child = open_login(window, directory, current)
        if child is not None:
            if controller.closed.is_set():
                child.destroy()
            else:
                login_windows.append(child)

    controller.on_napcat_login = open_plugin_login

    def close_windows() -> None:
        """主窗口关闭时一并关闭登录窗口，避免原生事件循环继续持有后端。"""
        controller.closed.set()
        for child in login_windows:
            child.destroy()

    window.events.closed += close_windows

    def restart() -> None:
        """原生菜单在工作线程调用，重启期间不阻塞窗口事件循环。"""
        try:
            controller.restart()
        except ValueError as exc:
            if not controller.closed.is_set():
                show_error(window, str(exc))

    def change_port() -> None:
        """弹窗保存仅开放端口字段，文件错误不会回显配置或中断当前服务。"""
        try:
            try:
                _, current = connection_settings(directory)
            except ValueError:
                # 无效端口导致启动失败时，仍允许用户用表单修复。
                current = 8080
            port = port_dialog(window, current)
            if port is not None:
                controller.restart(port=port)
        except ValueError as exc:
            if not controller.closed.is_set():
                show_error(window, str(exc))
        except OSError:
            if not controller.closed.is_set():
                show_error(window, "端口保存失败，请检查启动配置文件的写入权限。")

    try:
        # 明确使用现代内核，避免退回不支持 React 管理页面的旧 IE 内核。
        webview.start(restart, gui="edgechromium", private_mode=True, menu=[
            Menu("服务", [
                MenuAction("重启后端", restart), MenuAction("修改端口…", change_port),
            ]),
        ])
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    prepare_directory()
    raise SystemExit(run_backend() if sys.argv[1:] == ["--desktop-child"] else main())
