"""用独立数据库验证 Agent 人格增量、旧角色保留与新库结构一致。"""

import sqlite3

import pytest
from tests.unit.test_database_upgrade import legacy_database, query, resource  # noqa: F401

from agent_bot.database_upgrade import UpgradeError, upgrade_database


def old_agents(params):
    """构造缺少人格列的旧版 Agent，角色与模型、工具参数均保留原值。"""
    with sqlite3.connect(params.sqlite_path) as connection:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(agent_bot_agent)')}
        if 'personality_prompt' in columns:
            connection.execute('ALTER TABLE agent_bot_agent DROP COLUMN personality_prompt')
        connection.executemany(
            'INSERT INTO agent_bot_agent '
            '(code,name,description,system_prompt,model_id,tools,max_steps,options,enabled,'
            'revision,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            [(code, code, '原说明', role, 17, '["old-tool"]', 3, '{"temperature":0.2}',
              1, 7, '2026-01-01', '2026-01-02')
             for code, role in [('first', '原始角色\n  请原样保留'), ('second', '第二个角色')]],
        )
        return connection.execute('SELECT * FROM agent_bot_agent ORDER BY id').fetchall()


def test_unversioned_agent_upgrade_preserves_rows_and_matches_fresh_schema(
    legacy_database,  # noqa: F811
):
    """只放宽新增人格列的基线，迁移后非空默认值及所有旧字段与新库一致。"""
    before = old_agents(legacy_database)
    result = upgrade_database(legacy_database, read_resource=resource)
    assert 'agent-personality-v1' in result.applied
    with sqlite3.connect(legacy_database.sqlite_path) as connection, sqlite3.connect(
        ':memory:',
    ) as fresh:
        fresh.executescript(resource('agent_bot_init.sqlite.sql'))
        assert connection.execute('SELECT * FROM agent_bot_agent ORDER BY id').fetchall() == [
            (*row, '') for row in before
        ]
        assert connection.execute('PRAGMA table_info(agent_bot_agent)').fetchall() == (
            fresh.execute('PRAGMA table_info(agent_bot_agent)').fetchall()
        )
        connection.execute("UPDATE agent_bot_agent SET personality_prompt='独立人格' "
                           "WHERE code='first'")
    assert upgrade_database(legacy_database, read_resource=resource).applied == ()
    assert query(legacy_database, 'SELECT personality_prompt FROM agent_bot_agent ORDER BY id') == [
        ('独立人格',), ('',),
    ]


def test_complete_new_schema_records_migration_without_duplicate_alter(legacy_database):  # noqa: F811
    """完整新库已有人格列时只核验，不能重复 ADD COLUMN 导致首次启动失败。"""
    result = upgrade_database(legacy_database, read_resource=resource)
    assert 'agent-personality-v1' in result.applied
    assert upgrade_database(legacy_database, read_resource=resource).applied == ()


def test_unversioned_agent_missing_existing_field_is_rejected(legacy_database):  # noqa: F811
    """人格迁移不能借机接受缺少旧角色列的不完整数据库。"""
    old_agents(legacy_database)
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute('ALTER TABLE agent_bot_agent DROP COLUMN system_prompt')
    with pytest.raises(UpgradeError, match='agent_bot_agent'):
        upgrade_database(legacy_database, read_resource=resource)


def test_invalid_existing_personality_column_is_not_stamped_current(legacy_database):  # noqa: F811
    """同名可空列不能视为迁移完成，也不能自动改写未知 SQLite 表结构。"""
    old_agents(legacy_database)
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute('ALTER TABLE agent_bot_agent ADD COLUMN personality_prompt TEXT')
    with pytest.raises(UpgradeError, match='人格'):
        upgrade_database(legacy_database, read_resource=resource)


def test_same_version_database_still_runs_missing_personality_migration(legacy_database):  # noqa: F811
    """当前版本安装尚无新增流水时，仍按唯一迁移 ID 补齐人格列。"""
    from agent_bot import __version__
    from agent_bot.database_upgrade import MIGRATIONS

    # 首次只跑旧清单，再删除人格列，模拟本次功能发布前的同版本安装。
    upgrade_database(
        legacy_database, read_resource=resource,
        migrations=tuple(item for item in MIGRATIONS if item.name != 'agent-personality-v1'),
    )
    before = old_agents(legacy_database)
    result = upgrade_database(legacy_database, read_resource=resource)
    assert result.previous_version == result.current_version == __version__
    assert result.applied == ('agent-personality-v1',)
    assert query(legacy_database, 'SELECT * FROM agent_bot_agent ORDER BY id') == [
        (*row, '') for row in before
    ]
