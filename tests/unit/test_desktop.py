"""验证桌面后端的真实启动、退出与外部工作目录，避免遗留后台服务。"""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest


def test_frozen_working_directory_uses_executable(monkeypatch, tmp_path):
    """从其他目录启动 EXE 仍应读取程序旁的配置，不写入内部资源目录。"""
    from agent_bot.desktop import prepare_directory

    install = tmp_path / "安装目录"
    install.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(install / "AgentBot.exe"))
    assert prepare_directory() == install
    assert Path.cwd() == install


@pytest.mark.parametrize("occupy_port", [False, True])
def test_backend_reports_own_readiness_and_closes_on_parent_exit(tmp_path, occupy_port):
    """实际 HTTP 服务就绪才报告地址；端口冲突不可连接到已有服务。"""
    import socket

    from agent_bot.desktop import BackendProcess

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        if occupy_port:
            listener.listen()
        else:
            listener.close()
        code = (
            "from fastapi import FastAPI\n"
            "from agent_bot.desktop import serve\n"
            "app = FastAPI()\n"
            "@app.get('/')\n"
            "def index(): return {'desktop': True}\n"
            f"serve(app, '127.0.0.1', {port})\n"
        )
        backend = BackendProcess(
            [sys.executable, "-c", code], tmp_path, tmp_path / "backend.log"
        )
        backend.start()
        try:
            if occupy_port:
                with pytest.raises(RuntimeError, match="后端"):
                    backend.wait_ready(timeout=15)
            else:
                assert backend.wait_ready(timeout=15) == f"http://127.0.0.1:{port}/"
                with httpx.Client(trust_env=False) as client:
                    assert client.get(backend.url).json() == {"desktop": True}
        finally:
            backend.stop()
        assert backend.process.poll() is not None
        if not occupy_port:
            assert backend.process.returncode == 0
            assert "Application shutdown complete" in backend.log_path.read_text(encoding="utf-8")


def test_cli_entry_does_not_import_gui(tmp_path):
    """离线命令不依赖桌面库，也不因工作目录变化读取开发配置。"""
    entry = Path(__file__).resolve().parents[2] / "tools" / "windows_entry.py"
    result = subprocess.run(
        [sys.executable, str(entry), "--help"], cwd=tmp_path,
        env=os.environ | {"PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8", timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "database init" in result.stdout


def test_startup_failure_reports_safe_detail_and_last_stage(tmp_path):
    """启动失败应直接显示脱敏原因，不让用户只看到泛化等待文字。"""
    from agent_bot.desktop import BackendProcess

    code = (
        "import json\n"
        "print('AGENT_BOT_DESKTOP_STAGE ' + json.dumps('读取启动配置'), flush=True)\n"
        "print('AGENT_BOT_DESKTOP_ERROR ' + json.dumps('配置校验失败：CHAT_DB_USER'), flush=True)\n"
    )
    backend = BackendProcess([sys.executable, "-c", code], tmp_path, tmp_path / "failure.log")
    stages = []
    backend.start()
    try:
        with pytest.raises(RuntimeError, match="CHAT_DB_USER"):
            backend.wait_ready(on_status=stages.append)
        assert "读取启动配置" in stages
    finally:
        backend.stop()


def test_startup_timeout_identifies_waiting_stage(tmp_path):
    """不通的依赖不能无限等待，超时提示应保留最后收到的启动阶段。"""
    from agent_bot.desktop import BackendProcess

    code = (
        "import json, sys\n"
        "print('AGENT_BOT_DESKTOP_STAGE ' + json.dumps('准备数据库和队列'), flush=True)\n"
        "sys.stdin.read()\n"
    )
    backend = BackendProcess([sys.executable, "-c", code], tmp_path, tmp_path / "timeout.log")
    backend.start()
    try:
        with pytest.raises(RuntimeError, match="准备数据库和队列"):
            backend.wait_ready(timeout=2)
    finally:
        backend.stop()


def test_desktop_controller_restarts_on_new_port_and_releases_old_process(tmp_path, monkeypatch):
    """真实子进程重启后端并切换端口，旧服务退出且端口持久化。"""
    import socket

    from agent_bot.desktop import DesktopController

    monkeypatch.delenv("PORT", raising=False)
    monkeypatch.delenv("HOST", raising=False)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        initial_port = listener.getsockname()[1]
    (tmp_path / ".env").write_text(f"PORT={initial_port}\n", encoding="utf-8")
    code = (
        "from fastapi import FastAPI\n"
        "from dotenv import dotenv_values\n"
        "from agent_bot.desktop import serve\n"
        "serve(FastAPI(), '127.0.0.1', int(dotenv_values('.env')['PORT']))\n"
    )
    urls, statuses = [], []
    desktop = DesktopController(
        tmp_path, [sys.executable, "-c", code], statuses.append, urls.append,
    )
    try:
        desktop.restart()
        first = desktop.backend.process
        desktop.restart(port=initial_port)
        assert first.poll() == 0
        assert desktop.backend.process.pid != first.pid
        second = desktop.backend.process
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            with pytest.raises(ValueError, match=r"占用|使用"):
                desktop.restart(port=listener.getsockname()[1])
            assert second.poll() is None
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            new_port = listener.getsockname()[1]
        desktop.restart(port=new_port)
        assert second.poll() == 0
        assert urls[-1] == f"http://127.0.0.1:{new_port}/"
        assert f"PORT={new_port}" in (tmp_path / ".env").read_text("utf-8")
    finally:
        desktop.stop()
    assert desktop.backend.process.poll() == 0


def test_close_during_startup_cancels_pending_restart_without_orphan(tmp_path):
    """慢启动期间拒绝重复重启，关窗及时取消等待并停止所属子进程。"""
    import threading

    from agent_bot.desktop import DesktopController

    started = threading.Event()
    code = (
        "import sys\n"
        "print('AGENT_BOT_DESKTOP_STAGE \"waiting-test\"', flush=True)\n"
        "sys.stdin.read()\n"
    )

    def status(message):
        """子进程确实开始工作后再触发关闭，避免依赖固定睡眠。"""
        if "waiting-test" in message:
            started.set()

    desktop = DesktopController(tmp_path, [sys.executable, "-c", code], status, lambda _: None)
    worker = threading.Thread(target=desktop.restart)
    worker.start()
    try:
        assert started.wait(timeout=5)
        with pytest.raises(ValueError, match="正在启动或重启"):
            desktop.restart()
    finally:
        desktop.stop()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert desktop.backend.process.poll() == 0
