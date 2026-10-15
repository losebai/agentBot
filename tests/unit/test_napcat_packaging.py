"""固定载荷准备不联网、不混入用户数据、不覆盖已有输出。"""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from napcat_fakes import make_payload


def test_smoke_copy_keeps_pythonnet_runtime_but_excludes_qq(tmp_path):
    """验收不带 QQ 程序，但原生窗口仍须带齐 Python.NET 依赖。"""
    script = Path(__file__).resolve().parents[2] / "tools/windows_smoke_files.py"
    spec = importlib.util.spec_from_file_location("windows_smoke_files_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    bundle = tmp_path / "bundle"
    make_payload(bundle / "runtime/napcat")
    runtime = bundle / "_internal/pythonnet/runtime"
    runtime.mkdir(parents=True)
    (runtime / "Python.Runtime.dll").write_bytes(b"native-dependency")
    destination = tmp_path / "deployment"
    module.copy_without_qq_payload(bundle, destination)
    assert not (destination / "runtime").exists()
    assert (destination / "_internal/pythonnet/runtime/Python.Runtime.dll").read_bytes() == (
        b"native-dependency"
    )


def test_prepare_payload_copies_only_verified_clean_resources(tmp_path):
    """准备时自动写入固定版本，并拒绝覆盖输出或混入运行配置。"""
    script = Path(__file__).resolve().parents[2] / "tools/prepare_napcat_payload.py"
    spec = importlib.util.spec_from_file_location("prepare_napcat_payload", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = make_payload(tmp_path / "source")
    (source / "manifest.json").unlink()
    destination = tmp_path / "output"
    module.prepare_payload(source, destination)
    from agent_bot.napcat.payload import PayloadError, load_payload

    assert load_payload(destination).napcat_version == "v4.18.38"
    assert not (destination / "qq").exists()
    with pytest.raises(FileExistsError):
        module.prepare_payload(source, destination)
    (source / "shell/config").mkdir()
    (source / "shell/config/webui.json").write_text('{"token":"do-not-ship"}')
    with pytest.raises(PayloadError):
        module.prepare_payload(source, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_prepare_payload_cli_uses_pinned_versions_without_version_argument(tmp_path):
    """真实命令行无需填写版本，生成的载荷仍须通过固定版本校验。"""
    from agent_bot.napcat.payload import load_payload

    script = Path(__file__).resolve().parents[2] / "tools/prepare_napcat_payload.py"
    source = make_payload(tmp_path / "source")
    destination = tmp_path / "output"
    result = subprocess.run(
        [sys.executable, str(script), str(source), str(destination)],
        capture_output=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = load_payload(destination)
    assert payload.napcat_version == "v4.18.38"
    assert not (destination / "qq").exists()


@pytest.mark.parametrize("source_is_file", [False, True])
def test_prepare_payload_rejects_invalid_source_before_creating_output(tmp_path, source_is_file):
    """源目录缺失或误传文件时明确报错，不留下构建目录。"""
    script = Path(__file__).resolve().parents[2] / "tools/prepare_napcat_payload.py"
    spec = importlib.util.spec_from_file_location("prepare_invalid_source", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "source"
    if source_is_file:
        source.write_bytes(b"not-a-directory")
    destination = tmp_path / "build/payload"
    with pytest.raises(ValueError, match="载荷源目录不存在或不是目录"):
        module.prepare_payload(source, destination)
    assert not destination.parent.exists()


@pytest.mark.parametrize("missing", ["shell/napcat.mjs", "shell/NapCatWinBootMain.exe"])
def test_prepare_payload_reports_missing_entry_before_creating_output(tmp_path, missing):
    """错误定位到缺少的 Shell 入口，避免复制大文件后才发现层级错误。"""
    from agent_bot.napcat.payload import PayloadError

    script = Path(__file__).resolve().parents[2] / "tools/prepare_napcat_payload.py"
    spec = importlib.util.spec_from_file_location("prepare_missing_entry", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = make_payload(tmp_path / "source")
    (source / missing).unlink()
    destination = tmp_path / "build/payload"
    with pytest.raises(PayloadError, match=f"载荷源目录缺少固定入口：{missing}"):
        module.prepare_payload(source, destination)
    assert not destination.parent.exists()


def test_payload_preparation_rejects_output_inside_source_before_staging(tmp_path, monkeypatch):
    """目标在源内会造成递归复制，必须在创建暂存目录之前拒绝。"""
    script = Path(__file__).resolve().parents[2] / "tools/prepare_napcat_payload.py"
    spec = importlib.util.spec_from_file_location("prepare_overlap", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = make_payload(tmp_path / "source")

    def forbidden_staging(*args, **kwargs):
        """拦截旧实现的递归入口，避免红灯测试真的耗尽路径或磁盘。"""
        raise AssertionError("staging reached before overlap rejection")

    monkeypatch.setattr(module.tempfile, "TemporaryDirectory", forbidden_staging)
    with pytest.raises(ValueError):
        module.prepare_payload(source, source / "output")


def test_windows_build_rejects_payload_inside_cleared_output(tmp_path):
    """真实执行构建入口，重叠输入须在 npm、uv 或 PyInstaller 前拒绝。"""
    source_root = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    tools = repo / "tools"
    tools.mkdir(parents=True)
    script = tools / "build_windows.ps1"
    script.write_bytes((source_root / "tools/build_windows.ps1").read_bytes())
    payload = make_payload(repo / "dist/windows/AgentBot/runtime/napcat")
    result = subprocess.run([
        "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script),
        "-NapCatPayloadDirectory", str(payload),
    ], capture_output=True, timeout=20)
    assert result.returncode != 0
    assert b"NAPCAT_PAYLOAD_OUTPUT_OVERLAP" in result.stdout + result.stderr
    assert (payload / "manifest.json").exists()
