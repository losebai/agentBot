"""使用注册表替身定位 QQ，测试不读取真实安装登记或启动用户程序。"""

import sys
from contextlib import nullcontext
from types import SimpleNamespace

import pytest


def registry_stub(monkeypatch, value):
    """只替代 Windows 注册表边界，文件检查仍使用隔离目录。"""
    calls = []

    def open_key(*args):
        """记录实际查询的登记位置，禁止搜索其他用户数据。"""
        calls.append(args)
        return nullcontext(object())

    registry = SimpleNamespace(
        HKEY_LOCAL_MACHINE=1, KEY_READ=2, KEY_WOW64_32KEY=4,
        OpenKey=open_key, QueryValueEx=lambda key, name: (value, 1),
    )
    monkeypatch.setitem(sys.modules, "winreg", registry)
    return registry, calls


@pytest.mark.parametrize("quoted", [False, True])
def test_find_qq_from_official_installation_registration(tmp_path, monkeypatch, quoted):
    """读取官方使用的 32 位安装登记，支持带空格且可加引号的卸载路径。"""
    from agent_bot.napcat.qq_installation import find_qq_executable

    folder = tmp_path / "Installed QQ"
    folder.mkdir()
    executable = folder / "QQ.exe"
    executable.write_bytes(b"offline-placeholder")
    uninstall = str(folder / "Uninstall.exe")
    registry, calls = registry_stub(monkeypatch, f'"{uninstall}"' if quoted else uninstall)
    assert find_qq_executable() == executable
    assert calls == [(registry.HKEY_LOCAL_MACHINE,
                      r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
                      0, registry.KEY_READ | registry.KEY_WOW64_32KEY)]
    assert executable.read_bytes() == b"offline-placeholder"


@pytest.mark.parametrize("value", ["", "relative/Uninstall.exe", None])
def test_find_qq_rejects_invalid_registration(tmp_path, monkeypatch, value):
    """登记为空、类型错误或路径相对时明确失败，不搜索工作目录。"""
    from agent_bot.napcat.qq_installation import QQInstallationError, find_qq_executable

    registry_stub(monkeypatch, value)
    with pytest.raises(QQInstallationError):
        find_qq_executable()


def test_find_qq_rejects_stale_registration(tmp_path, monkeypatch):
    """仅有卸载登记但 QQ.exe 已不存在时，不能返回猜测的可执行路径。"""
    from agent_bot.napcat.qq_installation import QQInstallationError, find_qq_executable

    registry_stub(monkeypatch, str(tmp_path / "Uninstall.exe"))
    with pytest.raises(QQInstallationError):
        find_qq_executable()


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_find_qq_reports_missing_or_unreadable_registration(monkeypatch, error):
    """登记不存在或无权访问时保持明确失败，不静默改用其他来源。"""
    from agent_bot.napcat.qq_installation import QQInstallationError, find_qq_executable

    registry, _ = registry_stub(monkeypatch, "")

    def fail_open(*args):
        """模拟注册表错误，原文不得进入公开状态。"""
        raise error("private registry details")

    registry.OpenKey = fail_open
    with pytest.raises(QQInstallationError):
        find_qq_executable()
