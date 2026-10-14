"""桌面壳专用启动端口读写与原生弹窗，不向网页开放进程控制。"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

from dotenv import dotenv_values


def connection_settings(directory: Path, port_override: int | None = None) -> tuple[str, int]:
    """沿用启动文件及环境变量优先级，只读取监听地址和端口。"""
    values: dict[str, str | None] = {}
    for source in (
        dotenv_values(directory / ".env", interpolate=False),
        dotenv_values(directory / ".env.prod", interpolate=False), os.environ,
    ):
        values.update({key.casefold(): value for key, value in source.items()
                       if key.casefold() in {"host", "port"}})
    try:
        port = port_override if port_override is not None else int(values.get("port", "8080"))
        validate_port(port)
    except (TypeError, ValueError) as exc:
        raise ValueError("PORT 必须是 1-65535 的整数，请通过修改端口修正。") from exc
    return values.get("host") or "127.0.0.1", port


def validate_port(port: int) -> None:
    """检查桌面操作边界，拒绝布尔值及未经解析的文本。"""
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("端口必须是 1-65535 的整数。")


def check_port_available(host: str, port: int) -> None:
    """保存前探测新监听地址，失败时保留当前后端与配置。"""
    validate_port(port)
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET) as probe:
            if os.name == "nt":
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind((host, port))
    except OSError as exc:
        raise ValueError(f"端口 {port} 已被占用或无法使用，请选择其他端口。") from exc


def save_port(directory: Path, port: int) -> None:
    """原生端口与网页静态保存共用文件锁，避免并发替换时覆盖其他配置或秘密。"""
    from agent_bot.startup_settings import StartupSettingsError, StartupSettingsStore

    validate_port(port)
    if any(name.casefold() == "port" for name in os.environ):
        raise ValueError("PORT 由启动环境变量指定，请先移除该环境变量再通过弹窗修改。")
    store = StartupSettingsStore(directory, {"port"})
    try:
        state = store.read()
        with store.edit({"port": port}, expected_revision=state.revision):
            pass
    except StartupSettingsError as exc:
        raise ValueError(str(exc)) from exc


def port_dialog(window: Any, current: int) -> int | None:
    """在 WebView2 的原生 UI 线程显示端口表单，取消不修改配置。"""
    from System import Decimal, Func, Object
    from System.Drawing import Point, Size
    from System.Windows.Forms import (
        Button,
        DialogResult,
        Form,
        FormBorderStyle,
        FormStartPosition,
        Label,
        NumericUpDown,
    )

    result = None

    def show() -> None:
        """对所属窗口模态显示，释放原生表单后再在工作线程重启后端。"""
        nonlocal result
        form = Form()
        try:
            form.Text = "修改后端端口"
            form.ClientSize = Size(370, 150)
            form.FormBorderStyle = FormBorderStyle.FixedDialog
            form.StartPosition = FormStartPosition.CenterParent
            form.MaximizeBox = False
            form.MinimizeBox = False
            label = Label()
            label.Text = "后端端口，保存后自动重启"
            label.Location, label.AutoSize = Point(20, 20), True
            field = NumericUpDown()
            field.Location, field.Size = Point(20, 48), Size(325, 26)
            field.AccessibleName = "后端端口"
            field.Minimum, field.Maximum, field.Value = Decimal(1), Decimal(65535), Decimal(current)
            save = Button()
            save.Text, save.Location, save.Size = "保存并重启", Point(145, 103), Size(110, 28)
            save.DialogResult = DialogResult.OK
            cancel = Button()
            cancel.Text, cancel.Location, cancel.Size = "取消", Point(265, 103), Size(80, 28)
            cancel.DialogResult = DialogResult.Cancel
            for control in (label, field, save, cancel):
                form.Controls.Add(control)
            form.AcceptButton, form.CancelButton = save, cancel
            if form.ShowDialog(window.native) == DialogResult.OK:
                result = int(str(field.Value))
        finally:
            form.Dispose()

    window.native.Invoke(Func[Object](show))
    return result


def show_error(window: Any, message: str) -> None:
    """原生提示保留当前管理页面，不回显底层异常或配置内容。"""
    from System import Func, Object
    from System.Windows.Forms import MessageBox

    def show() -> None:
        """通过所属 UI 线程弹窗，避免跨线程操作 WinForms。"""
        MessageBox.Show(window.native, message, "AgentBot 服务")

    window.native.Invoke(Func[Object](show))
