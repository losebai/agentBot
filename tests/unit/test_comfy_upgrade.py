"""Comfy 插件一次性兼容升级使用临时库，禁止触碰实际部署数据。"""

import json
import sqlite3

import pytest
from tests.unit.test_database_upgrade import legacy_database, query, resource  # noqa: F401

from agent_bot.database_upgrade import upgrade_database


@pytest.mark.parametrize('enabled,plugins,expected', [
    (True, None, ['qqbot', 'comfy']),
    (True, [], ['comfy']),
    (True, ['bilibili', 'qqbot'], ['bilibili', 'qqbot', 'comfy']),
    (True, ['comfy'], ['comfy']),
    (False, ['qqbot'], ['qqbot']),
    (False, None, None),
])
@pytest.mark.parametrize('backend', ['sqlite', 'mysql'])
def test_existing_workflow_choice_migrates_once(
    legacy_database, enabled, plugins, expected, backend,  # noqa: F811
):
    """保存选择仅升级一次，之后手动停用不能被下次启动重新启用。"""
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute("INSERT INTO chat_setting_revision(id,revision) VALUES (1,7)")
        values = [('chat_local_workflow_enabled', json.dumps(enabled))]
        if plugins is not None:
            values.append(('app_enabled_plugins', json.dumps(plugins)))
        connection.executemany("INSERT INTO chat_setting(config_key,config_value) VALUES (?,?)",
                               values)
    params, options = legacy_database, {}
    if backend == 'mysql':
        from tests.unit.test_database_upgrade_mysql import mysql_factory

        from agent_bot.settings import DatabaseParams

        params = DatabaseParams(backend='mysql', database='offline')
        options['connect'] = mysql_factory(legacy_database)[0]
    result = upgrade_database(params, read_resource=resource, **options)
    assert 'comfy-plugin-v1' in result.applied
    rows = query(legacy_database,
                 "SELECT config_value FROM chat_setting WHERE config_key='app_enabled_plugins'")
    assert (json.loads(rows[0][0]) if rows else None) == expected
    changed = enabled and (plugins is None or 'comfy' not in plugins)
    assert query(legacy_database, 'SELECT revision FROM chat_setting_revision') == [(7 + changed,)]
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute("UPDATE chat_setting SET config_value='[]' "
                           "WHERE config_key='app_enabled_plugins'")
    assert upgrade_database(params, read_resource=resource, **options).applied == ()
    assert query(legacy_database, "SELECT config_value FROM chat_setting "
                 "WHERE config_key='app_enabled_plugins'") == ([('[]',)] if rows else [])


def test_same_software_version_still_applies_new_registered_migration(legacy_database):  # noqa: F811
    """旧 0.5.9 已登记版本的部署仍须执行新增 ID，不能仅凭程序版本跳过。"""
    from agent_bot.database_upgrade import MIGRATIONS

    upgrade_database(legacy_database, read_resource=resource, migrations=MIGRATIONS[:1])
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute("INSERT INTO chat_setting_revision(id,revision) VALUES (1,5)")
        connection.execute("INSERT INTO chat_setting(config_key,config_value) "
                           "VALUES ('chat_local_workflow_enabled','true')")
    result = upgrade_database(legacy_database, read_resource=resource)
    assert result.previous_version == result.current_version
    assert result.applied == ('comfy-plugin-v1',)
    assert query(legacy_database, "SELECT config_value FROM chat_setting "
                 "WHERE config_key='app_enabled_plugins'") == [('["qqbot","comfy"]',)]
