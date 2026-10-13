"""从独立检出目录检查发布输入，缺失资源必须在依赖安装前明确报告。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "tools/ci/check_build_inputs.py"


@pytest.mark.parametrize("present", [False, True])
def test_preflight_checks_forced_files_without_git_or_project_dependencies(tmp_path, present):
    """只运行标准库脚本；完整资源通过，遗漏 SQL 时返回精确文件名。"""
    source = "sql/2026-10-13-agent-model-routing.sql"
    (tmp_path / "pyproject.toml").write_text(
        '[tool.hatch.build.targets.wheel.force-include]\n'
        f'"{source}" = "agent_bot/resources/routing.sql"\n'
        '"src/static" = "agent_bot/static"\n',
        encoding="utf-8",
    )
    (tmp_path / "src/static").mkdir(parents=True)
    if present:
        (tmp_path / "sql").mkdir()
        (tmp_path / source).write_text("-- 离线资源检查，不执行 SQL。\n", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(tmp_path)],
        env={**os.environ, "PYTHONUTF8": "1"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    if present:
        assert result.returncode == 0, result.stderr
        assert "构建资源检查通过" in result.stdout
    else:
        assert result.returncode == 1
        assert source in result.stderr
        assert "构建资源缺失" in result.stderr
        assert "src/static" not in result.stderr
