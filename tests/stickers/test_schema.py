"""表情库增量、新库与双后端升级在隔离数据库上保持一致。"""

import ast
import importlib.util
import re
import sqlite3
from pathlib import Path
from zipfile import ZipFile

import pytest
import tomllib
from tests.unit.test_database_upgrade import legacy_database, query, resource  # noqa: F401
from tests.unit.test_database_upgrade_mysql import mysql_factory

from agent_bot.database_upgrade import MIGRATIONS, upgrade_database
from agent_bot.settings import DatabaseParams

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ('stickers-init.sql', 'stickers-init.sqlite.sql')


def test_sticker_increment_matches_fresh_database_and_preserves_legacy(legacy_database):  # noqa: F811
    """无版本旧库允许缺少新图库；升级不改写已有表，索引与新库相同。"""
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute('DROP TABLE IF EXISTS agent_bot_sticker')
    result = upgrade_database(legacy_database, read_resource=resource)
    assert result.applied[-1] == 'stickers-v1'
    assert query(legacy_database, 'SELECT value FROM private_business') == [('must-preserve',)]
    with sqlite3.connect(legacy_database.sqlite_path) as upgraded, sqlite3.connect(
        ':memory:',
    ) as fresh:
        fresh.executescript(resource('agent_bot_init.sqlite.sql'))
        for sql in ('PRAGMA table_info(agent_bot_sticker)',
                    'PRAGMA index_list(agent_bot_sticker)'):
            assert upgraded.execute(sql).fetchall() == fresh.execute(sql).fetchall()
    assert upgrade_database(legacy_database, read_resource=resource).applied == ()


def test_same_version_missing_stickers_migration_is_applied(legacy_database):  # noqa: F811
    """同为0.5.10也依迁移流水补齐图库，不能仅比较版本号。"""
    # 旧清单的最终核验使用当前完整脚本，因此先保留新表，再模拟旧部署缺表。
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.executescript(resource('stickers-init.sqlite.sql'))
    upgrade_database(legacy_database, read_resource=resource,
                     migrations=tuple(item for item in MIGRATIONS if item.name != 'stickers-v1'))
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute('DROP TABLE agent_bot_sticker')
    result = upgrade_database(legacy_database, read_resource=resource)
    assert result.previous_version == result.current_version == '0.5.10'
    assert result.applied == ('stickers-v1',)


def test_mysql_stickers_upgrade_is_registered_and_reentrant(legacy_database):  # noqa: F811
    """离线MySQL协议替身执行真实增量结构，第二次启动不重建或清空表。"""
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute('DROP TABLE IF EXISTS agent_bot_sticker')
    connect, state = mysql_factory(legacy_database)
    params = DatabaseParams(backend='mysql', database='offline-stickers')
    first = upgrade_database(params, connect=connect, read_resource=resource)
    assert first.applied[-1] == 'stickers-v1'
    assert upgrade_database(params, connect=connect, read_resource=resource).applied == ()
    assert state.closed == 2


def test_mysql_column_comments_and_increment_are_in_full_schema():
    """双后端完整初始化显式包含增量，MySQL新增字段全部带中文注释。"""
    for suffix in ('.sql', '.sqlite.sql'):
        assert resource('stickers-init' + suffix) in resource('agent_bot_init' + suffix)
    for line in resource('stickers-init.sql').splitlines():
        if re.match(r'    [a-z_]+ (?:CHAR|VARCHAR|TEXT|BIGINT|INT|BOOLEAN|DATETIME)', line):
            assert re.search(r"COMMENT '[^']*[\u4e00-\u9fff]", line)


def test_sticker_resources_explicitly_included_in_release():
    """两个后端增量显式进入wheel及源码包，不依赖Git忽略状态。"""
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text('utf-8'))
    targets = project['tool']['hatch']['build']['targets']
    for name in RESOURCES:
        assert (ROOT / 'sql' / name).is_file()
        assert targets['wheel']['force-include'][f'sql/{name}'] == f'agent_bot/resources/{name}'
    assert targets['sdist']['force-include']['sql'] == 'sql'


@pytest.mark.parametrize('missing', RESOURCES)
def test_package_checker_rejects_missing_sticker_resource(tmp_path, missing):
    """实际包检查器必须拒绝缺少任一后端图库增量的发布产物。"""
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
    spec = importlib.util.spec_from_file_location('sticker_package_checker', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match=missing):
        module.check(wheel)
