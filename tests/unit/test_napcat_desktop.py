"""原生操作使用当前实例快照，不能误用尚未重启的草稿。"""

import json
import subprocess
import sys
import threading

import pytest


def test_native_state_does_not_import_backend_without_console(tmp_path):
    """无控制台的窗口读取状态，不能触发后端日志与模型初始化。"""
    code = """
import sys
from pathlib import Path
sys.stdout = sys.stderr = None
from agent_bot.desktop import BackendProcess
root = Path(sys.argv[1])
(root / '.env').write_text('PORT=8080\\n', encoding='utf-8')
backend = BackendProcess([], root, root / 'backend.log')
assert not backend.napcat_snapshot().webui_ready
assert 'nonebot' not in sys.modules
"""
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], timeout=15)
    assert result.returncode == 0


def test_desktop_state_rejects_arbitrary_url_and_masks_no_secret():
    """私有通知也使用严格结构，不接受 URL 或未知状态。"""
    from pydantic import ValidationError

    from agent_bot.napcat.desktop_ui import NapCatDesktopState

    state = NapCatDesktopState(mode="managed", state="running", webui_ready=True, webui_port=6099)
    assert state.login_url() == "http://127.0.0.1:6099/webui/"
    with pytest.raises(ValidationError):
        NapCatDesktopState.model_validate({**state.model_dump(), "url": "https://example.com"})


@pytest.mark.skipif(sys.platform != "win32", reason="实际 Windows 私有握手")
def test_parent_job_handshake_and_safe_napcat_notifications(tmp_path):
    """启动握手不会被误当停机信号，通知不带凭据。"""
    from agent_bot.desktop import BackendProcess

    message = json.dumps({"mode": "managed", "state": "running", "webui_ready": True,
                          "webui_port": 6099, "error_code": None})
    code = ("import sys; assert sys.stdin.read(1)=='G'; "
            f"print('AGENT_BOT_NAPCAT_STATE '+{message!r},flush=True); "
            "print('AGENT_BOT_NAPCAT_OPEN_LOGIN',flush=True); "
            "print('AGENT_BOT_DESKTOP_READY http://127.0.0.1:8080/',flush=True); "
            "sys.stdin.read()")
    backend = BackendProcess([sys.executable, "-c", code], tmp_path, tmp_path / "backend.log",
                             desktop_handshake=True)
    requested = threading.Event()
    backend.on_napcat_login = requested.set
    backend.start()
    try:
        backend.wait_ready(timeout=8)
        assert backend.napcat_state.webui_port == 6099
        assert requested.wait(timeout=2)
    finally:
        backend.stop()
    assert backend.process.poll() == 0
    assert backend.napcat_state.webui_ready is False
    assert backend.napcat_state.state == "stopped"
    assert "AGENT_BOT_NAPCAT_OPEN_LOGIN" not in (tmp_path / "backend.log").read_text()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 父进程异常退出清理")
def test_abrupt_parent_exit_closes_owned_backend(tmp_path):
    """即使父进程来不及发 EOF，OS 关闭 Job 句柄也会清理所属后端。"""
    import subprocess

    import win32api
    import win32con
    import win32event

    marker = tmp_path / "backend.pid"
    child = ("import sys,time,os,pathlib; assert sys.stdin.read(1)=='G'; "
             f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid())); "
             "print('AGENT_BOT_DESKTOP_READY http://127.0.0.1:8080/',flush=True); "
             "time.sleep(90)")
    parent = ("from agent_bot.desktop import BackendProcess; from pathlib import Path; "
              "import sys; "
              f"p=BackendProcess([sys.executable,'-c',{child!r}],Path({str(tmp_path)!r}),"
              f"Path({str(tmp_path / 'test.log')!r}),desktop_handshake=True); "
              "p.start(); p.wait_ready(timeout=8); print('ready',flush=True); sys.stdin.read()")
    process = subprocess.Popen([sys.executable, "-c", parent], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, text=True)
    handle = None
    try:
        assert process.stdout.readline().strip() == "ready"
        handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, int(marker.read_text()))
        process.terminate()
        process.wait(timeout=8)
        assert win32event.WaitForSingleObject(handle, 8000) == win32event.WAIT_OBJECT_0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=8)
        process.stdin.close()
        process.stdout.close()
        if handle:
            handle.Close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 桌面入口实际启动")
def test_corrupt_payload_keeps_management_backend_available(tmp_path, monkeypatch):
    """首次包内载荷损坏也能进入管理页，错误通过只读托管状态展示。"""
    import os
    import socket
    import time

    import httpx

    from agent_bot.desktop import BackendProcess
    from agent_bot.static_config import STATIC_SETTING_NAMES

    for name in tuple(os.environ):
        if name.casefold() in STATIC_SETTING_NAMES or name.upper().startswith(
            ("CHAT_DB_", "CHAT_REDIS_", "CHAT_QUEUE_", "AGENT_BOT_", "ONEBOT_")
        ):
            monkeypatch.delenv(name)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    monkeypatch.setenv("PORT", str(port))
    broken = tmp_path / "runtime/napcat"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{}", encoding="utf-8")
    backend = BackendProcess([sys.executable, "-m", "agent_bot.desktop", "--desktop-child"],
                             tmp_path, tmp_path / "backend.log", desktop_handshake=True)
    backend.start()
    try:
        url = backend.wait_ready(timeout=20)
        with httpx.Client(base_url=url, trust_env=False) as client:
            assert client.get("/health/live").status_code == 200
            response = client.post("/api/v1/napcat/start", json={}, headers={
                "Origin": url.rstrip("/"),
            })
            assert response.status_code == 200
            limit = time.monotonic() + 5
            while time.monotonic() < limit:
                state = client.get("/api/v1/napcat/status").json()
                if state["state"] == "failed":
                    break
                time.sleep(0.02)
            assert state["error_code"] == "PAYLOAD_INVALID"
    finally:
        backend.stop()
