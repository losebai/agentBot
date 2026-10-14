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
    ],
)
def test_install_checks_download_before_execution(
    tmp_path: Path, scenario: str, succeeds: bool, runs_installer: bool
) -> None:
    """校验失败不得执行安装器；网络与进程失败须传递，成功和失败都释放临时文件。"""
    payload = b"isolated installer fixture"
    filename = "AgentBot-0.5.0-windows-x64-Setup.exe"
    (tmp_path / filename).write_bytes(payload)
    manifest = {
        "version": "0.5.0",
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
# 网络替身只读取隔离夹具；真实脚本仍负责解析、散列比对和资源释放。
function Invoke-WebRequest {
    param($Uri, $OutFile, [switch]$UseBasicParsing)
    $name = [IO.Path]::GetFileName(([uri]$Uri).AbsolutePath)
    if ($env:INSTALL_SCENARIO -eq 'download_failure' -and $OutFile) {
        throw 'fixture download failed'
    }
    $source = Join-Path $env:INSTALL_FIXTURES $name
    if ($OutFile) { Copy-Item -LiteralPath $source -Destination $OutFile }
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
            "TEMP": str(downloads),
        },
        capture_output=True, timeout=30,
    )
    assert (result.returncode == 0) == succeeds, result.stderr.decode(errors="replace")
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
        }[scenario]
        assert expected_error in result.stderr.decode(errors="replace")
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
