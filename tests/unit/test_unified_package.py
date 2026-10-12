"""验证统一命名空间在独立进程中的导入和资源读取能力。"""

import subprocess
import sys


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
