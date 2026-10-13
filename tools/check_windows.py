"""在临时部署目录验收 Windows 包，只使用临时 SQLite 和独立 Redis 替身。"""

import argparse
import base64
import ctypes
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import fakeredis
import httpx

from agent_bot.desktop import BackendProcess


def check_gui(deployment: Path, port: int, screenshot: Path) -> None:
    """通过此 WebView2 实例的调试端口验收真实桌面窗口，随后发送原生关闭消息。"""
    from websockets.sync.client import connect

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        debug_port = listener.getsockname()[1]
    started_at = time.perf_counter()
    process = subprocess.Popen(
        [str(deployment / "AgentBot.exe")], cwd=deployment.parent,
        env=os.environ | {
            "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={debug_port}"
        },
    )
    try:
        deadline = time.monotonic() + 60
        target = None
        with httpx.Client(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline and process.poll() is None:
                try:
                    pages = client.get(f"http://127.0.0.1:{debug_port}/json/list").json()
                    target = next((page for page in pages
                                   if page.get("url") == f"http://127.0.0.1:{port}/"), None)
                    if target:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.2)
        if target is None:
            raise RuntimeError("desktop window did not load the frontend")
        with connect(target["webSocketDebuggerUrl"], proxy=None) as connection:
            sequence = 0

            def call(method: str, params: dict) -> dict:
                """跳过异步 CDP 事件，只返回此次请求结果。"""
                nonlocal sequence
                sequence += 1
                connection.send(json.dumps({"id": sequence, "method": method, "params": params}))
                while True:
                    response = json.loads(connection.recv(timeout=10))
                    if response.get("id") == sequence:
                        assert "error" not in response, response
                        return response["result"]

            while time.monotonic() < deadline:
                result = call("Runtime.evaluate", {
                    "expression": "document.body.innerText", "returnByValue": True,
                })
                content = result["result"].get("value", "")
                if "供应商" in content and "Agent" in content:
                    break
                time.sleep(0.2)
            else:
                raise RuntimeError("desktop frontend did not render navigation")
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            data = call("Page.captureScreenshot", {"format": "png"})["data"]
            screenshot.write_bytes(base64.b64decode(data))
            print(
                f"Native desktop frontend rendered in {time.perf_counter() - started_at:.2f}s "
                "and screenshot captured.", flush=True,
            )
    finally:
        # 仅关闭本次测试进程所属的顶层窗口，不触碰用户的浏览器或已有服务。
        callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        @callback_type
        def close_window(handle, _parameter):
            """按 PID 限定 WM_CLOSE 的目标。"""
            owner = ctypes.c_ulong()
            ctypes.windll.user32.GetWindowThreadProcessId(handle, ctypes.byref(owner))
            if owner.value == process.pid:
                ctypes.windll.user32.PostMessageW(handle, 0x0010, 0, 0)
            return True

        ctypes.windll.user32.EnumWindows(close_window, 0)
        try:
            process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
    assert process.returncode == 0
    log = (deployment / "logs" / "desktop-backend.log").read_text(encoding="utf-8")
    assert "Application shutdown complete" in log
    print("Native window close stopped its backend gracefully.", flush=True)


def check(bundle: Path, screenshot: Path | None = None) -> None:
    """不读取开发配置，不连接 QQ/模型；检查 CLI、资源、API 和正常关闭。"""
    for name in ("AgentBot.exe", "agent-bot.exe", "_internal", ".env.example"):
        if not (bundle / name).exists():
            raise ValueError(f"missing bundle entry: {name}")
    package = bundle / "_internal" / "agent_bot"
    if not (package / "resources" / "sdxl_lightning_4step_api.json").is_file():
        raise ValueError("missing workflow resource")
    # 仅扫描本项目发布文件，第三方依赖可能合法包含名为 data 的资源目录。
    for path in package.rglob("*"):
        if path.name.startswith(".env") or path.suffix in {".db", ".sqlite3", ".log"}:
            raise ValueError(f"unexpected runtime file: {path.name}")

    redis = fakeredis.TcpFakeServer(("127.0.0.1", 0), server_type="redis")
    redis.daemon_threads = True
    threading.Thread(target=redis.serve_forever, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="agentbot-desktop-") as temporary:
            deployment = Path(temporary) / "desktop"
            shutil.copytree(bundle, deployment)
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            (deployment / ".env").write_text(
                "CHAT_DB_SQLITE_PATH=data/test.sqlite3\n"
                f"CHAT_REDIS_HOST=127.0.0.1\nCHAT_REDIS_PORT={redis.server_address[1]}\n"
                "CHAT_REDIS_KEY_PREFIX=desktop:qa\nAGENT_BOT_ADMIN_TOKEN=\n"
                "ONEBOT_V11_ACCESS_TOKEN=desktop-test-onebot-token-at-least-32-characters\n"
                "CHAT_SESSION_SECRET=desktop-test-session-secret-at-least-32-characters\n"
                f"HOST=127.0.0.1\nPORT={port}\n",
                encoding="utf-8",
            )
            executable = deployment / "agent-bot.exe"
            # 使用无关 cwd，确保 EXE 从自身目录读取配置及 SQL。
            for arguments in (["--help"], ["database", "init"], ["settings", "init"]):
                started_at = time.perf_counter()
                result = subprocess.run(
                    [str(executable), *arguments], cwd=temporary, capture_output=True,
                    env=os.environ | {"PYTHONIOENCODING": "utf-8"},
                    text=True, encoding="utf-8", errors="replace", timeout=45,
                )
                if result.returncode:
                    raise RuntimeError(f"CLI {arguments}: {result.stdout}\n{result.stderr}")
                print(
                    f"CLI verified: {' '.join(arguments)} "
                    f"({time.perf_counter() - started_at:.2f}s)", flush=True,
                )
            assert (deployment / "data" / "test.sqlite3").is_file()
            backend = BackendProcess(
                [str(executable), "--desktop-child"], Path(temporary),
                deployment / "logs" / "check.log",
            )
            started_at = time.perf_counter()
            backend.start()
            try:
                try:
                    url = backend.wait_ready(timeout=60)
                    print(f"Backend ready in {time.perf_counter() - started_at:.2f}s", flush=True)
                except RuntimeError:
                    print(backend.log_path.read_text(encoding="utf-8"), flush=True)
                    raise
                with httpx.Client(base_url=url, trust_env=False) as client:
                    page = client.get("/")
                    assert page.status_code == 200
                    assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', page.text)
                    assert assets
                    for asset in assets:
                        assert client.get(asset).status_code == 200
                    assert client.get("/api/v1/auth").json() == {"password_required": False}
                    assert client.get("/api/v1/settings").status_code == 200
                    assert client.get("/health/ready").status_code == 200
                print("Frozen backend, plugin, frontend assets and API verified.", flush=True)
            finally:
                backend.stop()
            assert backend.process.returncode == 0
            print("Graceful shutdown verified.", flush=True)
            if screenshot is not None:
                check_gui(deployment, port, screenshot)
    finally:
        redis.shutdown()
        redis.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--gui-screenshot", type=Path)
    arguments = parser.parse_args()
    # 避免开发机进程环境覆盖临时 .env，尤其不能继承真实数据库或外部连接配置。
    for key in tuple(os.environ):
        if key.upper().startswith(("CHAT_", "ONEBOT_", "AGENT_BOT_")) or key.upper() in {
            "HOST", "PORT", "DRIVER", "SUPERUSERS", "ENVIRONMENT",
        }:
            os.environ.pop(key)
    check(arguments.bundle.resolve(), arguments.gui_screenshot)
