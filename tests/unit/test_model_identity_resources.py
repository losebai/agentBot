"""发布包必须显式收集被Git忽略的模型增量，并拒绝遗漏升级所需资源。"""

import ast
import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = (
    "2026-10-20-model-identity.sql",
    "2026-10-20-model-identity.sqlite.sql",
    "UPGRADE-MODEL-IDENTITY.md",
)


def test_model_identity_resources_explicitly_included_for_wheel_and_sdist():
    """Git忽略不能让迁移脚本从发布中静默消失。"""
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    targets = project["tool"]["hatch"]["build"]["targets"]
    for name in RESOURCES:
        assert (ROOT / "sql" / name).is_file()
        assert targets["wheel"]["force-include"][f"sql/{name}"] == f"agent_bot/resources/{name}"
    assert targets["sdist"]["force-include"]["sql"] == "sql"


@pytest.mark.parametrize("missing", RESOURCES)
def test_package_checker_rejects_each_missing_model_migration_resource(tmp_path, missing):
    """构造除单一资源外完整的wheel，验证缺失错误确实来自该资源。"""
    source = ROOT / "tools/check_package.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    required = next(
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "required" for target in node.targets)
    )
    wheel = tmp_path / "missing.whl"
    with ZipFile(wheel, "w") as archive:
        for name in required - {f"agent_bot/resources/{missing}"}:
            archive.writestr(name, "")
    spec = importlib.util.spec_from_file_location("identity_package_checker", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match=missing):
        module.check(wheel)
