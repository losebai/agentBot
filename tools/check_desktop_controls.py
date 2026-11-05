"""用 Windows UI Automation 操作真实服务菜单和端口弹窗，只使用临时本地部署。"""

import argparse
import os
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
from windows_smoke_files import copy_without_qq_payload


def free_port() -> int:
    """为隔离验收选择临时端口，不占用用户固定端口。"""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def check(bundle: Path, screenshot: Path | None) -> None:
    """通过原生控件验证保存、取消、重启和关窗后的端口释放。"""
    import clr

    assemblies = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/WPF"
    clr.AddReference(str(assemblies / "UIAutomationClient.dll"))
    clr.AddReference(str(assemblies / "UIAutomationTypes.dll"))
    clr.AddReference("System.Windows.Forms")
    from System import Int32
    from System.Windows.Automation import (
        AutomationElement,
        ExpandCollapsePattern,
        InvokePattern,
        PropertyCondition,
        TreeScope,
        WindowPattern,
    )
    from System.Windows.Forms import SendKeys

    from agent_bot.desktop_settings import save_port
    from agent_bot.first_run import prepare_first_run

    with tempfile.TemporaryDirectory(prefix="agentbot-controls-") as temporary:
        deployment = Path(temporary) / "desktop"
        # 原生菜单验收只测试本项目后端，禁止意外启动真实 QQ。
        copy_without_qq_payload(bundle, deployment)
        prepare_first_run(deployment)
        initial, changed = free_port(), free_port()
        save_port(deployment, initial)
        command = [str(deployment / "taobot.exe")]
        process = subprocess.Popen(command, cwd=deployment)
        window = None

        def wait_for(read, *, timeout=60):
            """等待可观察的窗口或 HTTP 状态，不使用固定启动延时。"""
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                value = read()
                if value:
                    return value
                time.sleep(0.2)
            raise AssertionError("desktop controls check timed out")

        def named(parent, name):
            """仅在本次验收进程的控件树内按可访问名称查找。"""
            return parent.FindFirst(TreeScope.Descendants,
                                    PropertyCondition(AutomationElement.NameProperty, name))

        def invoke(element):
            """使用控件提供的 Invoke 模式点击，无屏幕坐标依赖。"""
            element.GetCurrentPattern(InvokePattern.Pattern).Invoke()

        def owned(name):
            """模态窗口及菜单浮层也按 PID 隔离，不能匹配用户其他程序的控件。"""
            roots = AutomationElement.RootElement.FindAll(
                TreeScope.Children,
                PropertyCondition(AutomationElement.ProcessIdProperty, Int32(process.pid)),
            )
            for root in roots:
                if root.Current.Name == name:
                    return root
                element = named(root, name)
                if element is not None:
                    return element
            return None

        def menu(label):
            """展开实际服务菜单并点击指定项目。"""
            item = wait_for(lambda: owned("服务"))
            if ExpandCollapsePattern.Pattern in item.GetSupportedPatterns():
                item.GetCurrentPattern(ExpandCollapsePattern.Pattern).Expand()
            else:
                invoke(item)
            invoke(wait_for(lambda: owned(label)))

        def ready(port):
            """确认新监听地址连同持久队列已经就绪。"""
            try:
                with httpx.Client(trust_env=False, timeout=1) as client:
                    return client.get(f"http://127.0.0.1:{port}/health/ready").status_code == 200
            except httpx.TransportError:
                return False

        try:
            window = wait_for(lambda: AutomationElement.RootElement.FindFirst(
                TreeScope.Children,
                PropertyCondition(AutomationElement.ProcessIdProperty, Int32(process.pid)),
            ))
            print("Native window found.", flush=True)
            wait_for(lambda: ready(initial))
            print("Initial backend ready.", flush=True)
            menu("重启后端")
            wait_for(lambda: not ready(initial))
            wait_for(lambda: ready(initial))
            print("Same-port restart ready.", flush=True)
            menu("修改端口…")
            dialog = wait_for(lambda: owned("修改后端端口"))
            before = (deployment / ".env").read_bytes()
            invoke(named(dialog, "取消"))
            wait_for(lambda: owned("修改后端端口") is None)
            assert (deployment / ".env").read_bytes() == before
            assert ready(initial)
            menu("修改端口…")
            dialog = wait_for(lambda: owned("修改后端端口"))
            # WinForms 旧版 UIA 代理不公开内部编辑框，聚焦本进程控件后模拟键盘填写。
            field = named(dialog, "后端端口")
            field.SetFocus()
            SendKeys.SendWait("^a")
            SendKeys.SendWait(str(changed))
            SendKeys.SendWait("{TAB}")
            if screenshot is not None:
                from PIL import ImageGrab

                rectangle = window.Current.BoundingRectangle
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                ImageGrab.grab(bbox=(int(rectangle.Left), int(rectangle.Top),
                                     int(rectangle.Right), int(rectangle.Bottom))).save(screenshot)
            invoke(named(dialog, "保存并重启"))
            wait_for(lambda: ready(changed))
            assert not ready(initial)
            assert f"PORT={changed}" in (deployment / ".env").read_text("utf-8")
            print("Native restart, cancel and saved port verified.", flush=True)
        finally:
            if window is not None:
                dialog = owned("修改后端端口")
                if dialog is not None:
                    dialog.GetCurrentPattern(WindowPattern.Pattern).Close()
                window.GetCurrentPattern(WindowPattern.Pattern).Close()
            try:
                process.wait(timeout=25)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=5)
        assert process.returncode == 0
        assert not ready(initial) and not ready(changed)
        print("Closing native window released backend ports.", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--screenshot", type=Path)
    args = parser.parse_args()
    for key in tuple(os.environ):
        if key.upper().startswith(("CHAT_", "ONEBOT_", "AGENT_BOT_")) or key.upper() in {
            "HOST", "PORT", "DRIVER", "SUPERUSERS", "ENVIRONMENT",
        }:
            os.environ.pop(key)
    check(args.bundle.resolve(), args.screenshot)
