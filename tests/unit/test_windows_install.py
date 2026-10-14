"""隔离验收一键安装脚本，替代网络和安装器进程，保留真实清单解析与文件校验。"""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="需要 Windows PowerShell")
SCRIPT = Path(__file__).resolve().parents[2] / "deploy" / "windows" / "install.ps1"


@pytest.mark.parametrize(
    ("scenario", "succeeds", "runs_installer"),
    [
        ("success", True, True),
        ("remote", True, True),
        ("bad_hash", False, False),
        ("bad_filename", False, False),
        ("download_failure", False, False),
        ("installer_failure", False, True),
        ("upgrade", True, True),
        ("same_version", True, False),
        ("newer_installed", True, False),
        ("numeric_upgrade", True, True),
        ("invalid_installed_version", False, False),
    ],
)
def test_install_checks_download_before_execution(
    tmp_path: Path, scenario: str, succeeds: bool, runs_installer: bool
) -> None:
    """只安装未装或更新版本，校验失败不执行；所有路径释放下载资源且不改写真实登记。"""
    payload = b"isolated installer fixture"
    release_version = "0.5.10" if scenario == "numeric_upgrade" else "0.5.0"
    installed_version = {
        "upgrade": "0.4.0",
        "same_version": "0.5.0",
        "newer_installed": "0.6.0",
        "numeric_upgrade": "0.5.9",
        "invalid_installed_version": "unknown",
    }.get(scenario, "")
    filename = f"AgentBot-{release_version}-windows-x64-Setup.exe"
    (tmp_path / filename).write_bytes(payload)
    manifest = {
        "version": release_version,
        "filename": filename,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    if scenario == "bad_hash":
        manifest["sha256"] = "0" * 64
    if scenario == "bad_filename":
        manifest["filename"] = "../unexpected.exe"
    (tmp_path / "latest.json").write_text(json.dumps(manifest), encoding="utf-8")
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    harness = tmp_path / "harness.ps1"
    harness.write_text(
        r"""
$ErrorActionPreference = 'Stop'
# 仅读取注册表替身，不创建或覆盖真实安装登记；其他路径继续使用系统文件检查。
function Test-Path {
    param($LiteralPath, $PathType)
    if ($LiteralPath -like 'HKCU:*') {
        $expectedKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
            '{379D6B51-BAED-48B4-A39D-FB1E5A424042}_is1'
        if ($LiteralPath -ne $expectedKey) { throw 'incorrect AgentBot installation key' }
        return [bool]$env:INSTALL_EXISTING_VERSION
    }
    if ($PathType) {
        Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath -PathType $PathType
    } else { Microsoft.PowerShell.Management\Test-Path -LiteralPath $LiteralPath }
}
function Get-ItemProperty {
    param($LiteralPath)
    [pscustomobject]@{ DisplayVersion = $env:INSTALL_EXISTING_VERSION }
}
# 网络替身只读取隔离夹具；真实脚本仍负责解析、散列比对和资源释放。
function Invoke-WebRequest {
    param($Uri, $OutFile, [switch]$UseBasicParsing)
    $name = [IO.Path]::GetFileName(([uri]$Uri).AbsolutePath)
    if ($env:INSTALL_SCENARIO -eq 'download_failure' -and $OutFile) {
        throw 'fixture download failed'
    }
    $source = Join-Path $env:INSTALL_FIXTURES $name
    if ($OutFile) {
        'downloaded' | Set-Content -LiteralPath (Join-Path $env:INSTALL_FIXTURES 'downloaded.txt')
        Copy-Item -LiteralPath $source -Destination $OutFile
    }
    else { [pscustomobject]@{ Content = [IO.File]::ReadAllText($source) } }
}
# 不在测试中启动真实安装器，记录传给进程的参数并模拟退出状态。
function Start-Process {
    param($FilePath, $ArgumentList, [switch]$Wait, [switch]$PassThru)
    if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) { throw 'missing installer' }
    $marker = Join-Path $env:INSTALL_FIXTURES 'started.txt'
    $ArgumentList -join ' ' | Set-Content -LiteralPath $marker -Encoding utf8
    if ($env:INSTALL_SCENARIO -eq 'installer_failure') { [pscustomobject]@{ ExitCode = 5 } }
    else { [pscustomobject]@{ ExitCode = 0 } }
}
try {
    if ($env:INSTALL_SCENARIO -eq 'remote') {
        Invoke-Expression ([IO.File]::ReadAllText($env:INSTALL_SCRIPT))
    } else {
        & $env:INSTALL_SCRIPT -BaseUrl 'https://fixtures.invalid/agentbot'
    }
    # 版本相同时仅返回安装脚本，不能直接退出用户正在使用的 PowerShell 终端。
    'continued' | Set-Content -LiteralPath (Join-Path $env:INSTALL_FIXTURES 'continued.txt')
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
""",
        encoding="utf-8-sig",
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
         "-File", str(harness)],
        # Python 不会像 PowerShell 7 那样为旧版子进程整理模块路径，让 5.1 自行发现系统模块。
        env={key: value for key, value in os.environ.items() if key.upper() != "PSMODULEPATH"} | {
            "INSTALL_SCRIPT": str(SCRIPT),
            "INSTALL_FIXTURES": str(tmp_path),
            "INSTALL_SCENARIO": scenario,
            "INSTALL_EXISTING_VERSION": installed_version,
            "TEMP": str(downloads),
        },
        capture_output=True, timeout=30,
    )
    assert (result.returncode == 0) == succeeds, result.stderr.decode(errors="replace")
    assert (tmp_path / "continued.txt").exists() == succeeds
    marker = tmp_path / "started.txt"
    assert marker.exists() == runs_installer
    if runs_installer:
        arguments = marker.read_text(encoding="utf-8-sig")
        assert "/SILENT" in arguments
        assert "/NORESTART" in arguments
    if not succeeds:
        expected_error = {
            "bad_hash": "SHA256 verification failed",
            "bad_filename": "Invalid AgentBot release manifest",
            "download_failure": "fixture download failed",
            "installer_failure": "exit code 5",
            "invalid_installed_version": "Invalid installed AgentBot version",
        }[scenario]
        assert expected_error in result.stderr.decode(errors="replace")
    if scenario in {"same_version", "newer_installed", "invalid_installed_version"}:
        assert not (tmp_path / "downloaded.txt").exists()
    if scenario in {"upgrade", "numeric_upgrade"}:
        expected_update = f"Updating AgentBot {installed_version} -> {release_version}"
        assert expected_update in result.stdout.decode()
    if scenario == "same_version":
        assert b"already up to date" in result.stdout
    if scenario == "newer_installed":
        assert b"Keeping installed version" in result.stdout
    assert list(downloads.iterdir()) == []


def test_cmd_download_failure_returns_error_and_removes_partial_script(tmp_path: Path) -> None:
    """让 curl 连接不可用的本机代理，验证 CMD 的失败状态与临时文件释放。"""
    result = subprocess.run(
        ["cmd.exe", "/d", "/c", str(SCRIPT.with_suffix(".bat"))],
        env=os.environ | {
            "TEMP": str(tmp_path),
            "HTTPS_PROXY": "http://127.0.0.1:1",
            "NO_PROXY": "",
        },
        capture_output=True, timeout=15,
    )
    assert result.returncode != 0
    assert b"Failed to download AgentBot install script" in result.stdout
    assert list(tmp_path.iterdir()) == []
