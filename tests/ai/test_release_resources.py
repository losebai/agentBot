import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest


def checker():
    path = Path(__file__).parents[2] / "tools" / "check_package.py"
    spec = importlib.util.spec_from_file_location("package_checker", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.check


def test_package_rejects_missing_ai_sql(tmp_path):
    wheel = tmp_path / "offline.whl"
    with ZipFile(wheel, "w") as archive:
        for name in (
            "agent_bot/__main__.py",
            "agent_bot/application.py",
            "agent_bot/static/index.html",
            "agent_bot/bot_cli.py",
            "agent_bot/runtime_fields.py",
            "agent_bot/resources/sdxl_lightning_4step_api.json",
            "agent_bot/static/assets/app.js",
        ):
            archive.writestr(name, "")
    with pytest.raises(ValueError, match="resources missing"):
        checker()(wheel)
