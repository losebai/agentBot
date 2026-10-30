"""演化增量使用临时业务库，核对新旧库结构及数据保留。"""

import json
import sqlite3

import pytest
from tests.unit.test_database_upgrade import legacy_database, resource  # noqa: F401

from agent_bot.database_upgrade import EVOLUTION_TABLES, upgrade_database
from agent_bot.settings import DatabaseParams


def test_existing_database_adds_evolution_without_rewriting_data(legacy_database):  # noqa: F811
    """完整旧库缺少演化表时由显式清单补齐，第二次启动无重复 DDL。"""
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        for table in ('agent_bot_evolution_event', 'agent_bot_evolution_profile',
                      'agent_bot_evolution_config'):
            connection.execute(f'DROP TABLE {table}')
    first = upgrade_database(legacy_database, read_resource=resource)
    assert 'agent-evolution-v1' in first.applied
    with sqlite3.connect(legacy_database.sqlite_path) as connection, sqlite3.connect(
        ':memory:',
    ) as new:
        new.executescript(resource('agent_bot_init.sqlite.sql'))
        assert connection.execute('SELECT value FROM private_business').fetchall() == [
            ('must-preserve',),
        ]
        for table in EVOLUTION_TABLES:
            assert connection.execute(f'PRAGMA table_info({table})').fetchall() == (
                new.execute(f'PRAGMA table_info({table})').fetchall()
            )
            assert connection.execute(f'PRAGMA foreign_key_list({table})').fetchall() == (
                new.execute(f'PRAGMA foreign_key_list({table})').fetchall()
            )
            assert connection.execute(f'PRAGMA index_list({table})').fetchall() == (
                new.execute(f'PRAGMA index_list({table})').fetchall()
            )
    assert upgrade_database(legacy_database, read_resource=resource).applied == ()


@pytest.mark.parametrize('backend', ['sqlite', 'mysql'])
def test_independent_upgrade_preserves_existing_ids_and_evidence(
    legacy_database, backend,  # noqa: F811
):
    """旧范围数据复制到独立结构，事件与因果引用保持原编号且重入不覆盖。"""
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        for table in ('agent_bot_evolution_record', 'agent_bot_evolution_state',
                      'agent_bot_evolution_scope'):
            connection.execute(f'DROP TABLE {table}')
        connection.execute(
            'INSERT INTO agent_bot_agent '
            '(id,code,name,description,system_prompt,model_id,tools,max_steps,options,enabled,'
            'revision,created_at,updated_at) VALUES '
            "(13,'legacy','旧角色','','原角色',17,'[]',3,'{}',1,4,'2026-01-01','2026-01-01')",
        )
        connection.execute('INSERT INTO agent_bot_evolution_config VALUES (13,0,6)')
        connection.execute('INSERT INTO agent_bot_evolution_profile '
                           '(id,agent_id,subject_key,traits,revision) VALUES (41,13,?,?,3)',
                           ('a' * 64, json.dumps({'verbosity': -1, 'warmth': 0,
                                                 'humor': 0, 'initiative': 0})))
        connection.executemany(
            'INSERT INTO agent_bot_evolution_event '
            '(id,profile_id,source_key,kind,dimension,direction,excerpt,observed_at,consumed,'
            'before_value,after_value,evidence_ids) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            [(91, 41, 'source', 'feedback', 'verbosity', -1, '以后请简洁一点',
              '2026-01-01', 1, None, None, '[]'),
             (92, 41, 'change', 'change', 'verbosity', -1, '原变化原因',
              '2026-01-08', 0, 0, -1, '[91]')],
        )
    params = legacy_database
    options = {}
    if backend == 'mysql':
        from tests.unit.test_database_upgrade_mysql import mysql_factory

        options['connect'], _ = mysql_factory(legacy_database)
        params = DatabaseParams(backend='mysql', database='offline')
    result = upgrade_database(params, read_resource=resource, **options)
    assert 'evolution-independent-v2' in result.applied
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        scope = connection.execute('SELECT scope_key,agent_id,enabled,revision '
                                   'FROM agent_bot_evolution_scope WHERE agent_id=13').fetchone()
        assert scope == (
            'agent:13', 13, 0, 6,
        )
        assert connection.execute('SELECT id,scope_key,revision '
                                  'FROM agent_bot_evolution_state').fetchall() == [
            (41, 'agent:13', 3),
        ]
        assert connection.execute('SELECT id,profile_id,evidence_ids '
                                  'FROM agent_bot_evolution_record ORDER BY id').fetchall() == [
            (91, 41, '[]'), (92, 41, '[91]'),
        ]
        assert connection.execute('SELECT COUNT(*) FROM agent_bot_evolution_event').fetchone() == (
            2,
        )
        # 验证应用更新后的新值不能被独立增量的重复执行回填覆盖。
        connection.execute("UPDATE agent_bot_evolution_record SET excerpt='后续记录' WHERE id=92")
        connection.executescript(resource('evolution-independent.sqlite.sql'))
        assert connection.execute('SELECT excerpt FROM agent_bot_evolution_record '
                                  'WHERE id=92').fetchone() == ('后续记录',)
    assert upgrade_database(params, read_resource=resource, **options).applied == ()
