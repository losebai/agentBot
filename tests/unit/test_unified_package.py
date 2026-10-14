"""验证统一命名空间在独立进程中的导入和资源读取能力。"""

import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest


@pytest.mark.parametrize("editable", [False, True], ids=["wheel", "editable"])
def test_installed_package_resources_share_wheel_layout(tmp_path: Path, editable: bool) -> None:
    """隔离安装真实包，开发映射须保留源码导入及模板、SQL 的正式资源路径。"""
    root = Path(__file__).resolve().parents[2]
    target = tmp_path / "site-packages"
    command = ["uv", "pip", "install", "--offline", "--no-deps", "--target", str(target)]
    if editable:
        command.append("--editable")
    command.append(str(root))
    installed = subprocess.run(command, cwd=root, capture_output=True, text=True)
    assert installed.returncode == 0, installed.stderr
    # 不加载当前环境的 editable .pth，仅读取本次隔离安装的入口；现有依赖无需重复安装。
    dependencies = sorted({sysconfig.get_path("purelib"), sysconfig.get_path("platlib")})
    code = (
        "import json, site, sys\n"
        f"sys.path.insert(0, {str(target)!r})\n"
        f"sys.path.extend({dependencies!r})\n"
        f"site.addsitedir({str(target)!r})\n"
        "from importlib.resources import files\n"
        "from importlib.metadata import version\n"
        "from pathlib import Path\n"
        "import agent_bot\n"
        "import agent_bot.config\n"
        "assert agent_bot.__version__ == version('agent-bot')\n"
        "resources = files('agent_bot').joinpath('resources')\n"
        "template = json.loads(resources.joinpath('sdxl_lightning_4step_api.json')"
        ".read_text(encoding='utf-8'))\n"
        "assert template['1']['class_type'] == 'CheckpointLoaderSimple'\n"
        "assert 'CREATE TABLE' in resources.joinpath('agent_bot_init.sqlite.sql')"
        ".read_text(encoding='utf-8')\n"
    )
    if editable:
        code += (
            "assert Path(agent_bot.config.__file__).resolve() == "
            f"Path({str(root / 'src/agent_bot/config.py')!r}).resolve()\n"
        )
    result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unified_package_imports_and_loads_workflow_without_startup() -> None:
    """包迁移后无需初始化 NoneBot 或连接数据库即可导入并读取内置工作流。"""
    code = (
        "import json\n"
        "from importlib.resources import files\n"
        "import agent_bot.application\n"
        "import agent_bot.bot_cli\n"
        "from agent_bot.config import RuntimeConfig\n"
        "from agent_bot.services.chat import ChatService\n"
        "from agent_bot.services.workflows import _load_sdxl_api_template\n"
        "assert json.loads(_load_sdxl_api_template())\n"
        "resource = files('agent_bot').joinpath('resources/sdxl_lightning_4step_api.json')\n"
        "assert json.loads(resource.read_text(encoding='utf-8'))\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
