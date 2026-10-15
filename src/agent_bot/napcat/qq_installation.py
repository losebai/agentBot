"""按官方 Shell 使用的 Windows 安装登记定位 QQ，不扫描磁盘或读取登录资料。"""

from pathlib import Path


class QQInstallationError(ValueError):
    """本机 QQ 未安装、登记无效或程序已移除，调用边界仅公开固定错误码。"""


def find_qq_executable() -> Path:
    """只读取官方 32 位卸载登记，验证 QQ.exe 后交给所属进程启动。"""
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
            0, winreg.KEY_READ | winreg.KEY_WOW64_32KEY,
        ) as key:
            uninstall, _ = winreg.QueryValueEx(key, "UninstallString")
        if not isinstance(uninstall, str) or not uninstall.strip():
            raise QQInstallationError("QQ 安装登记无效。")
        uninstaller = Path(uninstall.strip().strip('"'))
        if not uninstaller.is_absolute():
            raise QQInstallationError("QQ 安装登记必须使用绝对路径。")
        executable = uninstaller.parent / "QQ.exe"
        if not executable.is_file():
            raise QQInstallationError("QQ 安装目录缺少 QQ.exe。")
        return executable
    except OSError as exc:
        raise QQInstallationError("无法读取本机 QQ 安装登记或程序。") from exc
