"""Windows 桌面壳及其专属后端进程；复用管理界面和正常应用生命周期。"""

from __future__ import annotations

import html
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

_READY = "AGENT_BOT_DESKTOP_READY "
_STAGE = "AGENT_BOT_DESKTOP_STAGE "
_ERROR = "AGENT_BOT_DESKTOP_ERROR "


def prepare_directory() -> Path:
    """冻结程序以 EXE 所在目录读取配置和保存数据，源码入口保留当前目录。"""
    if getattr(sys, "frozen", False):
        os.chdir(Path(sys.executable).resolve().parent)
    return Path.cwd()


class BackendProcess:
    """通过私有标准输入管道管理本窗口启动的后端，不接管其他服务。"""

    def __init__(self, command: list[str], directory: Path, log_path: Path) -> None:
        """登记启动路径；构造时不启动服务、不创建业务数据。"""
        self.command = command
        self.directory = directory
        self.log_path = log_path
        self.process: subprocess.Popen[str] | None = None
        self.url: str | None = None
        self.stage = "加载后端组件"
        self.error: str | None = None
        self._started_at = 0.0
        self._ready = threading.Event()
        self.finished = threading.Event()

    def start(self) -> None:
        """隐藏后端控制台，并将输出持续写入外部日志。"""
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
        threading.Thread(target=self._read_output, daemon=True).start()

    def _read_output(self) -> None:
        """就绪只接受所属子进程的通知，避免端口冲突时误开其他实例。"""
        assert self.process is not None and self.process.stdout is not None
        try:
            with self.log_path.open("w", encoding="utf-8") as log, self.process.stdout:
                for line in self.process.stdout:
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
            self.finished.set()
            self._ready.set()

    def wait_ready(
        self, timeout: float = 60, *, on_status: Callable[[str], None] | None = None,
    ) -> str:
        """从进程启动时计时并同步阶段，配置失败不再只给出泛化的等待提示。"""
        previous = None
        while True:
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


def serve(app: Any, host: str, port: int) -> None:
    """运行同一 ASGI 应用；父进程退出或关闭管道时执行完整 shutdown。"""
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
                print(f"{_READY}http://{address}:{port}/", flush=True)

    server = DesktopServer(uvicorn.Config(app, host=host, port=port, log_level="info"))

    def watch_parent() -> None:
        """EOF 同时覆盖正常关窗和桌面进程异常退出，不开放额外控制端口。"""
        sys.stdin.read(1)
        server.should_exit = True

    threading.Thread(target=watch_parent, daemon=True).start()
    server.run()


def run_backend() -> int:
    """桌面后端加载现有配置；不创建数据库，不读取或覆盖动态环境配置。"""
    # 冻结解释器可能忽略 PYTHONIOENCODING，明确匹配父进程的 UTF-8 日志解码。
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    print(_STAGE + json.dumps("加载后端组件"), flush=True)
    from agent_bot.admin.config import AdminConfig
    from agent_bot.bootstrap import load_startup_settings
    from agent_bot.cli_errors import describe_config_error
    from agent_bot.settings import SettingsError

    try:
        print(_STAGE + json.dumps("读取启动配置和数据库"), flush=True)
        admin = AdminConfig.from_environment()
        stored = load_startup_settings()
    except (ValueError, SettingsError) as exc:
        print(_ERROR + json.dumps(describe_config_error(exc)), flush=True)
        return 1
    print(_STAGE + json.dumps("装配机器人和管理接口"), flush=True)
    from agent_bot.application import create_application

    app = create_application(stored, admin)
    print(_STAGE + json.dumps("准备数据库和队列"), flush=True)
    serve(app, str(stored.config.host), stored.config.port)
    return 0


def status_page(message: str) -> str:
    """无服务时也显示清晰状态；错误文字转义后展示，不注入脚本。"""
    return (
        '<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
        '<style>body{font:16px system-ui;margin:64px;color:#303030;background:#fafafa}'
        'h1{font-size:24px}p{line-height:1.8;white-space:pre-wrap}</style>'
        f"<h1>AgentBot</h1><p>{html.escape(message)}</p></html>"
    )


def main() -> int:
    """打开独立 WebView2 窗口，关窗或 GUI 失败时释放所属后端。"""
    import webview

    directory = prepare_directory()
    command = (
        [str(directory / "agent-bot.exe"), "--desktop-child"]
        if getattr(sys, "frozen", False)
        else [sys.executable, "-m", "agent_bot.desktop", "--desktop-child"]
    )
    backend = BackendProcess(command, directory, directory / "logs" / "desktop-backend.log")
    closed = threading.Event()
    window = webview.create_window(
        "AgentBot", html=status_page("正在启动服务…"), width=1280, height=860,
        min_size=(800, 600), text_select=True,
    )
    window.events.closed += closed.set

    def show_status(stage: str) -> None:
        """启动过程显示可定位的阶段，关窗后不再操作已销毁的 WebView。"""
        if not closed.is_set():
            window.load_html(status_page(f"正在启动服务…\n{stage}"))

    def load_frontend() -> None:
        """GUI 就绪后等待后端；启动错误留在窗口中，便于读取日志和初始化。"""
        try:
            url = backend.wait_ready(on_status=show_status)
            if closed.is_set():
                return
            window.load_url(url)
            backend.finished.wait()
            if not closed.is_set():
                window.load_html(status_page("后端已停止，请查看 logs/desktop-backend.log。"))
        except RuntimeError as exc:
            backend.stop()
            if not closed.is_set():
                window.load_html(status_page(
                    f"{exc}\n详情：logs/desktop-backend.log\n"
                    "首次使用请按 WINDOWS_README.md 配置并执行初始化命令。"
                ))

    try:
        backend.start()
        # 明确使用现代内核，避免退回不支持 React 管理页面的旧 IE 内核。
        webview.start(load_frontend, gui="edgechromium", private_mode=True)
    finally:
        closed.set()
        backend.stop()
    return 0


if __name__ == "__main__":
    prepare_directory()
    raise SystemExit(run_backend() if sys.argv[1:] == ["--desktop-child"] else main())
