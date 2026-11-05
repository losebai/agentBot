"""媒体发布资源、版本、无副作用导入及回退字段边界。"""

import json
import subprocess
import sys
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]


def test_media_sql_packaged_and_runtime_files_excluded():
    """发布包含新脚本，排除运行文件和验收环境。"""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build = project["tool"]["hatch"]["build"]
    resources = build["targets"]["wheel"]["force-include"]
    assert resources["sql/agent_bot_init.sql"] == "agent_bot/resources/agent_bot_init.sql"
    for name in ("agent_bot_media_init.sql", "2026-10-12-agentbot-schema-comments.sql"):
        source = f"sql/legacy/{name}" if name == "agent_bot_media_init.sql" else f"sql/{name}"
        assert resources[source] == f"agent_bot/resources/{name}"
    assert "/data" in build["exclude"]
    assert "/.superpowers" in build["exclude"]


def test_release_versions_match_frontend_and_python():
    """包、源码和界面使用同一版本号，旧 wheel 不覆盖。"""
    import agent_bot

    package = json.loads((ROOT / "web/package.json").read_text(encoding="utf-8"))
    assert agent_bot.__version__ == package["version"] == "0.6.3"
    assert (ROOT / "dist/agent_bot-0.3.0-py3-none-any.whl").is_file()


def test_media_import_does_not_initialize_io(tmp_path):
    """全新解释器导入媒体及 API 不启动插件、不建目录、不连接数据库。"""
    code = """
import importlib, pathlib, socket, sys
def forbidden(*a, **kw):
    raise AssertionError('禁止导入触发 I/O')
socket.socket.connect = forbidden
pathlib.Path.mkdir = forbidden
for name in ('config','contracts','catalog','inputs','download','files','store','records',
             'resources','providers','service','validation'):
    importlib.import_module('agent_bot.media.' + name)
importlib.import_module('agent_bot.admin.media_routes')
assert 'agent_bot.plugin.application' not in sys.modules
print('媒体模块导入无启动副作用')
"""
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code], cwd=tmp_path, capture_output=True, text=True,
        encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
