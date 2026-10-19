"""验证 UTC 执行时间与北京时间历史记录的排序和幂等校正。"""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from agent_bot import record_migration
from agent_bot.ai.call_records import CallRecordStore, decision_record, legacy_record, python_record
from agent_bot.ai.contracts import SessionWrite, TokenUsage
from agent_bot.ai.sessions import AiSessionStore
from agent_bot.database import database_connector
from agent_bot.settings import DatabaseParams


@pytest.mark.parametrize('stamp', [
    datetime(2026, 10, 13, 18, 30, 0, 123456),
    '2026-10-13 18:30:00.123456',
    datetime(2026, 10, 14, 2, 30, tzinfo=timezone(timedelta(hours=8))),
])
@pytest.mark.parametrize('kind', ['ordinary', 'agent', 'auto_speak'])
def test_native_archive_converts_utc_to_beijing(stamp, kind):
    """UTC 晚间跨日归档后仍表示同一时刻，带时区输入不得重复偏移。"""
    row = {
        'execution_id': 'a' * 32, 'id': 'b' * 32, 'session_id': 's', 'request_id': 'r',
        'created_at': stamp, 'prompt': '输入', 'content': '回复', 'duration_ms': 1,
        'model': 'm', 'status': 'completed', 'mcp_code': 'decision', 'mcp_revision': 1,
        'should_speak': 1, 'confidence': 0.8, 'error_code': None,
    }
    record = decision_record(row) if kind == 'auto_speak' else python_record(row, kind)
    assert record['create_time'] == datetime(2026, 10, 14, 2, 30)


async def test_new_qq_call_sorts_before_beijing_history(ai_params, ai_database):
    """新调用不能因 UTC 时间较小而落到同日历史记录后，分页和详情保持一致。"""
    store = CallRecordStore(ai_params, connect=ai_database.connect)
    await store.import_batch([legacy_record({
        'id': 1, 'create_time': '2026-10-13 13:49:47', 'data': {'toolCalls': []},
    }, 'legacy')])
    await AiSessionStore(ai_params, connect=ai_database.connect).save(SessionWrite(
        execution_id='a' * 32, session_id='qq', request_id='qq-new',
        configured_model='alias', model='m', status='completed', duration_ms=1,
        usage=TokenUsage(), created_at=datetime(2026, 10, 13, 9, 51, 23),
        prompt='输入', content='回复',
    ))
    first = await store.list_calls(limit=1)
    assert first.items[0].request_id == 'qq-new'
    assert first.items[0].create_time == datetime(2026, 10, 13, 17, 51, 23)
    assert (await store.get_call(first.items[0].id)).create_time == first.items[0].create_time
    second = await store.list_calls(limit=1, cursor=first.next_cursor)
    assert second.items[0].source == 'legacy' and second.next_cursor is None
    assert ai_database.connection.execute(
        'SELECT created_at_utc FROM agent_bot_call_record '
        'WHERE source=\'python:agent_bot_ai_session\''
    ).fetchone()[0] == '2026-10-13 09:51:23'


@pytest.mark.parametrize('backend', ['mysql-double', 'sqlite'])
async def test_repair_uses_original_utc_and_is_repeatable(
    ai_params, ai_database, tmp_path, backend,
):
    """校正只改变可关联原始记录的时间，重跑不重复加八小时或覆盖正文。"""
    store = CallRecordStore(ai_params, connect=ai_database.connect)
    ai_database.connection.executescript('''
        CREATE TABLE agent_bot_agent_run (execution_id TEXT, created_at TEXT);
        CREATE TABLE agent_bot_auto_speak_decision (id TEXT, created_at TEXT);
        INSERT INTO agent_bot_ai_session (execution_id, created_at)
            VALUES ('a', '2026-10-13 09:51:23.123456');
        INSERT INTO agent_bot_agent_run VALUES ('b', '2026-10-13 18:30:00');
        INSERT INTO agent_bot_auto_speak_decision VALUES ('c', '2026-10-13 09:00:00');
    ''')
    rows = []
    for key, table in [('a', 'ai_session'), ('b', 'agent_run'), ('c', 'auto_speak_decision')]:
        row = legacy_record({'id': key, 'create_time': '2026-10-13 09:00:00',
                             'content': '保留输入', 'replay_context': '保留回复'},
                            f'python:agent_bot_{table}')
        rows.append(row)
    rows.append(legacy_record({'id': 'old', 'create_time': '2026-10-13 13:00:00'}, 'legacy'))
    await store.import_batch(rows)
    if backend == 'sqlite':
        path = tmp_path / 'records.sqlite3'
        with sqlite3.connect(path) as target:
            ai_database.connection.backup(target)
        params = DatabaseParams(backend='sqlite', sqlite_path=str(path))
        store = CallRecordStore(params, connect=database_connector(params))
    repair = getattr(record_migration, 'repair_local_times', None)
    assert repair is not None, '缺少显式校正已有 UTC 调用记录的命令'
    result = await repair(store, batch_size=1)
    assert sum(row['updated'] for row in result.values()) == 3
    page = await store.list_calls()
    assert [(r.source_id, r.create_time) for r in page.items] == [
        ('b', datetime(2026, 10, 14, 2, 30)),
        ('a', datetime(2026, 10, 13, 17, 51, 23)),
        ('c', datetime(2026, 10, 13, 17)),
        ('old', datetime(2026, 10, 13, 13)),
    ]
    detail = await store.get_call(page.items[0].id)
    assert (detail.content, detail.replay_context) == ('保留输入', '保留回复')
    repeated = await repair(store, batch_size=1)
    assert sum(row['updated'] for row in repeated.values()) == 0


def test_repair_cli_rejects_import_options():
    """校正命令拒绝初始化或旧库导入，防止额外修改数据库。"""
    with pytest.raises(SystemExit) as error:
        record_migration.run(['repair-times', '--init-schema'])
    assert error.value.code == 2
