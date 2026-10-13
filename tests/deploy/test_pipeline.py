"""验证发布依赖图及失败回滚，不运行真实 Docker 或 SSH。"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def bash_path():
    """Windows 使用 Git 自带 Bash，Linux 使用系统 Bash。"""
    if os.name == "nt":
        git = shutil.which("git")
        assert git, "部署脚本测试需要 Git Bash"
        return str(Path(git).parents[1] / "bin/bash.exe")
    return shutil.which("bash") or "/bin/bash"


def shell_path(path):
    """向 Git Bash 传递 POSIX 路径，路径只来自测试临时目录。"""
    value = Path(path).resolve().as_posix()
    return f"/{value[0].lower()}{value[2:]}" if os.name == "nt" else value


def test_ci_checks_gate_packaging_and_protected_manual_deployment():
    """发布必须依赖完整检查，部署不能被 MR 或未保护引用自动触发。"""
    pipeline = yaml.safe_load((ROOT / ".gitlab-ci.yml").read_text("utf-8"))
    assert set(pipeline["package"]["needs"]) == {
        "python-check",
        "frontend-check",
        "deployment-check",
    }
    assert pipeline["container-image"]["needs"] == ["package"]
    deploy = pipeline["deploy-production"]
    assert deploy["needs"] == ["container-image"]
    assert deploy["resource_group"] == "agentbot-production"
    assert deploy["interruptible"] is False
    assert deploy["rules"][0]["when"] == "manual"
    assert deploy["rules"][0]["allow_failure"] is False
    assert 'CI_COMMIT_REF_PROTECTED == "true"' in deploy["rules"][0]["if"]
    script = "\n".join(pipeline["python-check"]["script"])
    assert "database_onebot_token_reaches_adapter_without_log_disclosure" in script
    assert "--cov-fail-under=0" not in script and "--no-cov" not in script
    compose = yaml.safe_load((ROOT / "deploy/compose.yaml").read_text("utf-8"))
    assert set(compose["services"]) == {"agentbot"}
    assert compose["services"]["agentbot"]["read_only"] is True
    assert compose["volumes"]["agentbot-data"]["name"] == "agentbot-data"


def test_shell_scripts_parse_without_running_commands():
    """Bash 只进行语法检查，不解析凭据、不连接服务器。"""
    subprocess.run([bash_path(), "-n", shell_path(ROOT / "deploy/deploy.sh")], check=True)
    subprocess.run([bash_path(), "-n", shell_path(ROOT / "tools/ci/deploy-via-ssh.sh")], check=True)


@pytest.mark.parametrize("failure", ["none", "pull", "health"])
def test_deploy_preserves_or_rolls_back_current_release(tmp_path, failure):
    """以 Docker 替身验证成功切换、拉取失败和健康失败三个真实 shell 分支。"""
    base, binaries = tmp_path / "app", tmp_path / "bin"
    base.mkdir()
    binaries.mkdir()
    (base / "runtime.env").write_text("OFFLINE=true\n", "utf-8")
    for number in (1, 2):
        release = base / "releases" / str(number)
        release.mkdir(parents=True)
        (release / "compose.yaml").write_text("services: {}\n", "utf-8")
        (release / "release.env").write_text(
            f"AGENTBOT_IMAGE=registry.example/agentbot@sha256:{'a' * 64}\n"
            f"AGENTBOT_RUNTIME_ENV_FILE={shell_path(base)}/runtime.env\n",
            "utf-8",
        )
    fake = binaries / "docker"
    fake.write_text(
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$DOCKER_CALL_LOG"
if [[ "$*" == *"releases/2/"* ]]; then
  [[ "$FAILURE" == pull && "$*" == *" pull "* ]] && exit 1
  [[ "$FAILURE" == health && "$*" == *" up "* ]] && exit 1
fi
exit 0
""",
        "utf-8",
    )
    fake.chmod(0o755)
    if os.name == "nt":
        # Git Bash 没有 flock；Windows 这里只测试发布分支，Linux CI 使用真正文件锁。
        (binaries / "flock").write_text("#!/usr/bin/env bash\nexit 0\n", "utf-8")
    log = tmp_path / "docker-calls.txt"
    environment = {
        **os.environ,
        "PATH": f"{shell_path(binaries)}:/usr/bin:/bin",
        "DOCKER_CALL_LOG": shell_path(log),
        "FAILURE": failure,
        "MSYS": "winsymlinks:sys",
    }
    subprocess.run(
        [bash_path(), "-c", 'ln -s releases/1 "$1/current"', "--", shell_path(base)],
        env=environment,
        check=True,
    )
    result = subprocess.run(
        [bash_path(), shell_path(ROOT / "deploy/deploy.sh"), shell_path(base), "2"],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    current = subprocess.run(
        [bash_path(), "-c", 'readlink "$1/current"', "--", shell_path(base)],
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    calls = log.read_text("utf-8")
    if failure == "none":
        assert result.returncode == 0, result.stderr
        assert current == "releases/2"
    else:
        assert result.returncode != 0
        assert current == "releases/1"
    if failure == "health":
        assert any("releases/1/" in line and " up " in line for line in calls.splitlines())
    if failure == "pull":
        assert " up " not in calls
    assert " down " not in calls and "volume rm" not in calls
