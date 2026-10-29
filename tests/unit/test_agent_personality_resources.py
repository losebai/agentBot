"""人格迁移必须随两种发布包收集，wheel 缺任一后端脚本时明确失败。"""

import ast
import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest
import tomllib

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ('agent-personality.sql', 'agent-personality.sqlite.sql')


def test_personality_resources_explicitly_included_in_release():
    """忽略规则不能导致任一后端的必要人格增量遗漏。"""
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text('utf-8'))
    targets = project['tool']['hatch']['build']['targets']
    for name in RESOURCES:
        assert (ROOT / 'sql' / name).is_file()
        assert targets['wheel']['force-include'][f'sql/{name}'] == f'agent_bot/resources/{name}'
    assert targets['sdist']['force-include']['sql'] == 'sql'


@pytest.mark.parametrize('missing', RESOURCES)
def test_package_checker_rejects_missing_personality_resource(tmp_path, missing):
    """从实际检查器获取其余必需项，执行真实检查以验证缺资源的具体错误。"""
    source = ROOT / 'tools/check_package.py'
    required = next(
        ast.literal_eval(node.value)
        for node in ast.walk(ast.parse(source.read_text('utf-8')))
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == 'required' for target in node.targets)
    )
    wheel = tmp_path / 'missing.whl'
    with ZipFile(wheel, 'w') as archive:
        for name in required - {f'agent_bot/resources/{missing}'}:
            archive.writestr(name, '')
    spec = importlib.util.spec_from_file_location('personality_package_checker', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match=missing):
        module.check(wheel)
