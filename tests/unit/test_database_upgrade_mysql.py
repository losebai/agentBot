"""用 SQLite 协议替身核对 MySQL 命名锁、隐式提交和中断记录，不连接 MySQL。"""

import re
import sqlite3
from types import SimpleNamespace

import pymysql
import pytest
from tests.unit.test_database_upgrade import (  # noqa: F401
    legacy_database,
    query,
    released_resource,
    resource,
)

from agent_bot import __version__
from agent_bot.database_upgrade import MIGRATIONS, Migration, UpgradeError, upgrade_database
from agent_bot.settings import DatabaseParams
from agent_bot.sql_scripts import split_sql
from agent_bot.sqlite_database import SqliteConnection


def mysql_factory(
    params, *, lock=1, fail_once=False, fail_personality=None, fail_classification=False,
    fail_autonomy=False,
):
    """只翻译登记脚本的协议差异，其余读写由真实 SQLite 事务执行。"""
    # 新库的人格列沿用实际 MySQL 定义，避免替身把错误默认值或类型伪装正确。
    definition = re.search(r'^  personality_prompt (.+),$', resource('agent_bot_init.sql'), re.M)
    if definition:
        native = definition[1].split(' COMMENT ', 1)[0]
        with sqlite3.connect(params.sqlite_path) as schema_connection:
            original = schema_connection.execute(
                "SELECT sql FROM sqlite_master WHERE name='agent_bot_agent'",
            ).fetchone()[0]
            changed = original.replace("personality_prompt TEXT NOT NULL DEFAULT ''",
                                       f'personality_prompt {native}')
            schema_connection.execute('PRAGMA writable_schema=ON')
            schema_connection.execute("UPDATE sqlite_master SET sql=? WHERE name='agent_bot_agent'",
                                      (changed,))
            schema_connection.execute('PRAGMA writable_schema=OFF')
    comfy_translations = dict(zip(
        split_sql(resource('2026-10-28-comfy-plugin.sql')),
        split_sql(resource('2026-10-28-comfy-plugin.sqlite.sql')), strict=True,
    ))
    independent_translations = dict(zip(
        [s for s in split_sql(resource('evolution-independent.sql')) if s.startswith('INSERT')],
        [s for s in split_sql(resource('evolution-independent.sqlite.sql'))
         if s.startswith('INSERT')], strict=True,
    ))
    classification_translations = dict(zip(
        split_sql(resource('call-record-classification.sql')),
        split_sql(resource('call-record-classification.sqlite.sql')), strict=True,
    ))
    with sqlite3.connect(":memory:") as parser:
        parser.executescript(resource("startup-versioning.sqlite.sql"))
        parser.executescript(resource("bilibili-init.sqlite.sql"))
        parser.executescript(resource("material-classification.sqlite.sql"))
        parser.executescript(resource("stickers-init.sqlite.sql"))
        parser.executescript(resource("agent-evolution.sqlite.sql"))
        parser.executescript(resource("job-recovery.sqlite.sql"))
        parser.executescript(resource("persistent-agent.sqlite.sql"))
        for statement in split_sql(resource('evolution-independent.sqlite.sql')):
            if statement.startswith('CREATE'):
                parser.execute(statement)
        translations = dict(parser.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"))
        autonomy_indexes = {}
        for table, statement in parser.execute(
            "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL "
            "AND tbl_name LIKE 'agent_bot_autonomy_%'"
        ):
            autonomy_indexes.setdefault(table, []).append(statement)
    state = SimpleNamespace(events=[], closed=0, fail_once=fail_once,
                            fail_personality=fail_personality,
                            fail_classification=fail_classification, fail_autonomy=fail_autonomy)

    class Connection:
        """每次启动独占一个连接，关闭代表会话锁释放。"""

        def __init__(self):
            """保存真实事务对象。"""
            self.inner = SqliteConnection(
                params.sqlite_path, cursorclass=pymysql.cursors.DictCursor
            )

        def cursor(self):
            """读取 MySQL 元数据与持久化语句共享同一连接。"""
            return Cursor(self)

        def begin(self):
            """保持 SQLite 自己的事务隔离，不靠 mock 假装持久化。"""
            self.inner.begin()

        def commit(self):
            """提交使 DDL 中断后的状态能被下一连接观察。"""
            self.inner.commit()

        def rollback(self):
            """撤销本次未提交语句。"""
            self.inner.rollback()

        def close(self):
            """记录命名锁所属连接的释放。"""
            state.closed += 1
            self.inner.close()

    class Cursor:
        """只支持升级器实际发出的 MySQL 协议差异。"""

        def __init__(self, connection):
            """转交原始游标，不新增生产代码测试开关。"""
            self.owner = connection
            self.inner = connection.inner.cursor()
            self.rows = None

        def __enter__(self):
            """进入底层游标作用域。"""
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            """统一清理底层游标。"""
            return self.inner.__exit__(*args)

        def execute(self, sql, values=None):
            """模拟元数据、一次性故障及 MySQL DDL 隐式提交。"""
            state.events.append(sql)
            self.rows = None
            sql = comfy_translations.get(sql, sql)
            sql = independent_translations.get(sql, sql)
            sql = classification_translations.get(sql, sql)
            if "GET_LOCK" in sql:
                self.rows = [{"acquired": lock}]
                return
            if "information_schema.columns" in sql:
                if "COLUMN_NAME='system_kind'" in sql:
                    self.rows = [
                        {"extra": "VIRTUAL GENERATED" if row[6] == 2 else "",
                         "column_type": "varchar(16)" if row[2] == "TEXT" else row[2]}
                        for row in self.owner.inner._db.execute(
                            'PRAGMA table_xinfo(agent_bot_call_record)'
                        ) if row[1] == 'system_kind'
                    ]
                    return
                if "COLUMN_NAME='personality_prompt'" in sql:
                    columns = self.owner.inner._db.execute('PRAGMA table_info(agent_bot_agent)')
                    self.rows = [
                        {"data_type": row[2].lower(), "is_nullable": "NO" if row[3] else "YES",
                         "column_default": row[4]}
                        for row in columns if row[1] == 'personality_prompt'
                    ]
                    return
                names = [
                    row[0]
                    for row in self.owner.inner._db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                ]
                self.rows = [
                    {"table_name": name, "column_name": row[1]}
                    for name in names
                    for row in self.owner.inner._db.execute(f'PRAGMA table_xinfo("{name}")')
                ]
                return
            if "information_schema.statistics" in sql:
                self.rows = [
                    {"index_name": index[1], "non_unique": not index[2],
                     "position": column[0] + 1, "column_name": column[2], "sub_part": None}
                    for index in self.owner.inner._db.execute(f'PRAGMA index_list("{values[0]}")')
                    for column in self.owner.inner._db.execute(f'PRAGMA index_info("{index[1]}")')
                ]
                return
            if "information_schema.key_column_usage" in sql:
                self.rows = [
                    {"column_name": row[3], "referenced_table": row[2], "referenced_column": row[4]}
                    for row in self.owner.inner._db.execute(
                        f'PRAGMA foreign_key_list("{values[0]}")')
                ]
                return
            if sql.startswith("SHOW INDEX"):
                if "idx_call_system_time" in sql:
                    self.rows = [
                        {"Column_name": row[2], "Seq_in_index": row[0] + 1}
                        for row in self.owner.inner._db.execute(
                            'PRAGMA index_info(idx_call_system_time)'
                        )
                    ]
                    return
                self.rows = [
                    {"Non_unique": 0, "Column_name": value, "Seq_in_index": index}
                    for index, value in enumerate(("provider_id", "upstream_model"), 1)
                ]
                return
            if sql.startswith("ALTER TABLE agent_bot_call_record"):
                self.inner.execute(sql, values)
                self.owner.commit()
                if state.fail_classification:
                    state.fail_classification = False
                    raise pymysql.OperationalError(1205, 'offline interrupted after classification')
                return
            if sql.startswith('ALTER TABLE agent_bot_agent'):
                if 'ADD COLUMN personality_prompt' in sql:
                    self.inner.execute(sql.split(' COMMENT ', 1)[0], values)
                    self.owner.commit()
                    if state.fail_personality == 'after-add':
                        state.fail_personality = None
                        raise pymysql.OperationalError(1205, 'offline interrupted after ADD')
                    return
                if 'MODIFY COLUMN personality_prompt' in sql:
                    if state.fail_personality == 'before-not-null':
                        state.fail_personality = None
                        self.owner.commit()
                        raise pymysql.OperationalError(1205, 'offline interrupted before MODIFY')
                    # SQLite 无 MODIFY COLUMN，测试替身只重现该列的非空元数据变化。
                    self.owner.commit()
                    schema = self.owner.inner._db.execute(
                        "SELECT sql FROM sqlite_master WHERE name='agent_bot_agent'",
                    ).fetchone()[0]
                    changed = schema.replace('personality_prompt MEDIUMTEXT NULL',
                                             'personality_prompt MEDIUMTEXT NOT NULL')
                    self.owner.inner._db.execute('PRAGMA writable_schema=ON')
                    self.owner.inner._db.execute(
                        "UPDATE sqlite_master SET sql=? WHERE name='agent_bot_agent'", (changed,),
                    )
                    version = self.owner.inner._db.execute('PRAGMA schema_version').fetchone()[0]
                    self.owner.inner._db.execute(f'PRAGMA schema_version={version + 1}')
                    self.owner.inner._db.execute('PRAGMA writable_schema=OFF')
                    self.owner.commit()
                    if state.fail_personality == 'after-not-null':
                        state.fail_personality = None
                        raise pymysql.OperationalError(1205, 'offline interrupted after MODIFY')
                    return
            if sql.startswith("ALTER TABLE agent_bot_ai_provider"):
                if "ADD COLUMN user_agent" in sql:
                    self.inner.execute(
                        "ALTER TABLE agent_bot_ai_provider "
                        "ADD COLUMN user_agent TEXT NOT NULL DEFAULT ''"
                    )
                    self.owner.commit()
                    return
            if sql.startswith("CREATE TABLE"):
                name = re.search(r"CREATE TABLE (?:IF NOT EXISTS )?(\w+)", sql)[1]
                if state.fail_once and "agent_bot_bilibili_operation" in sql:
                    state.fail_once = False
                    raise pymysql.OperationalError(1205, "offline injected DDL failure")
                if "ENGINE=InnoDB" in sql:
                    name = re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", sql)[1]
                    sql = translations[name].replace(
                        "CREATE TABLE ", "CREATE TABLE IF NOT EXISTS ", 1
                    )
                self.inner.execute(sql, values)
                for statement in autonomy_indexes.get(name, ()):
                    self.inner.execute(statement.replace(
                        'CREATE INDEX ', 'CREATE INDEX IF NOT EXISTS ', 1))
                self.owner.commit()
                if state.fail_autonomy and name == 'agent_bot_autonomy_task':
                    state.fail_autonomy = False
                    raise pymysql.OperationalError(1205, 'offline autonomy DDL interruption')
                return
            self.inner.execute(sql, values)

        def fetchall(self):
            """元数据为假，所有业务记录均读取真实库。"""
            return self.rows if self.rows is not None else self.inner.fetchall()

        def fetchone(self):
            """与 MySQL DictCursor 一致返回映射或 None。"""
            return self.rows[0] if self.rows else self.inner.fetchone()

    return lambda **kwargs: Connection(), state


def test_mysql_failed_nonreentrant_step_keeps_old_version_and_stops_replay(
    legacy_database,  # noqa: F811
):
    """DDL 已提交也不能把软件版本提升；下一启动不能盲重放非幂等语句。"""
    params = DatabaseParams(backend="mysql", database="offline")
    connect, state = mysql_factory(legacy_database)
    upgrade_database(params, connect=connect, read_resource=resource)
    bad = Migration("0.6.4", "mysql-broken", "bad.sql", "bad.sql")

    def read(name):
        """第一个 DDL 成功后，第二条语句失败。"""
        return (
            "CREATE TABLE partial_test(id INTEGER); INSERT INTO missing VALUES (1);"
            if name == "bad.sql"
            else resource(name)
        )

    with pytest.raises(UpgradeError, match="mysql-broken"):
        upgrade_database(
            params,
            connect=connect,
            read_resource=read,
            target_version="0.6.4",
            migrations=(*MIGRATIONS, bad),
        )
    assert query(legacy_database, "SELECT version FROM agent_bot_schema_version") == [
        (__version__,),
    ]
    assert query(legacy_database, "SELECT name FROM sqlite_master WHERE name='partial_test'")
    assert query(
        legacy_database,
        "SELECT status FROM agent_bot_schema_migration WHERE migration_id='mysql-broken'",
    ) == [("failed",)]
    with pytest.raises(UpgradeError, match="不可重入"):
        upgrade_database(
            params,
            connect=connect,
            read_resource=read,
            target_version="0.6.4",
            migrations=(*MIGRATIONS, bad),
        )
    assert state.closed == 3


def test_mysql_known_idempotent_step_resumes_after_partial_ddl(legacy_database):  # noqa: F811
    """B站的已审查 CREATE IF NOT EXISTS 步骤允许从记录过的失败恢复。"""
    params = DatabaseParams(backend="mysql", database="offline")
    connect, state = mysql_factory(legacy_database, fail_once=True)
    with pytest.raises(UpgradeError, match="bilibili-v1"):
        upgrade_database(params, connect=connect, read_resource=resource)
    assert not query(legacy_database, "SELECT version FROM agent_bot_schema_version")
    result = upgrade_database(params, connect=connect, read_resource=resource)
    assert result.applied == (
        "bilibili-v1", "comfy-plugin-v1", "material-classification-v1", "agent-personality-v1",
        "stickers-v1", "agent-evolution-v1", "evolution-independent-v2",
        "provider-user-agent-v1", "job-recovery-v1", "call-record-classification-v1",
        "persistent-agent-v1",
    )
    assert state.closed == 2
    assert sum("GET_LOCK" in event for event in state.events) == 2


def test_mysql_lock_timeout_does_not_execute_any_ddl(legacy_database):  # noqa: F811
    """未获得独占升级锁时只能失败，不能继续建版本表。"""
    connect, state = mysql_factory(legacy_database, lock=0)
    with pytest.raises(UpgradeError, match="升级锁"):
        upgrade_database(
            DatabaseParams(backend="mysql", database="offline"),
            connect=connect,
            read_resource=resource,
        )
    assert not any(event.startswith("CREATE") for event in state.events)
    assert state.closed == 1


@pytest.mark.parametrize('failure', ['after-add', 'before-not-null', 'after-not-null'])
def test_mysql_personality_resumes_partial_ddl_without_touching_roles(
    legacy_database, failure,  # noqa: F811
):
    """模拟人格 ADD 或回填已隐式提交，重启仅恢复缺失步骤并保留非空人格。"""
    from tests.unit.test_agent_personality_upgrade import old_agents

    before = old_agents(legacy_database)
    connect, state = mysql_factory(legacy_database, fail_personality=failure)
    params = DatabaseParams(backend='mysql', database='offline')
    with pytest.raises(UpgradeError, match='agent-personality-v1'):
        upgrade_database(params, connect=connect, read_resource=resource)
    assert query(legacy_database, 'SELECT status FROM agent_bot_schema_migration '
                 "WHERE migration_id='agent-personality-v1'") == [('failed',)]
    assert not query(legacy_database, 'SELECT version FROM agent_bot_schema_version')
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute("UPDATE agent_bot_agent SET personality_prompt='保留人格' WHERE id=1")
    result = upgrade_database(params, connect=connect, read_resource=resource)
    assert result.applied == ('agent-personality-v1', 'stickers-v1', 'agent-evolution-v1',
                              'evolution-independent-v2', 'provider-user-agent-v1',
                              'job-recovery-v1', 'call-record-classification-v1',
                              'persistent-agent-v1')
    assert query(legacy_database, 'SELECT * FROM agent_bot_agent ORDER BY id') == [
        (*before[0], '保留人格'), (*before[1], ''),
    ]
    assert sum('ADD COLUMN personality_prompt' in event for event in state.events) == 1
    assert upgrade_database(params, connect=connect, read_resource=resource).applied == ()


def test_mysql_fresh_personality_schema_skips_alter_and_has_no_text_default(
    legacy_database,  # noqa: F811
):
    """读取实际 MySQL 初始化列定义的新库只登记流水，不使用不兼容的文本默认值。"""
    connect, state = mysql_factory(legacy_database)
    result = upgrade_database(DatabaseParams(backend='mysql', database='offline'), connect=connect,
                              read_resource=resource)
    assert 'agent-personality-v1' in result.applied
    assert not any('ALTER TABLE agent_bot_agent' in event for event in state.events)
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        personality = next(row for row in connection.execute('PRAGMA table_info(agent_bot_agent)')
                           if row[1] == 'personality_prompt')
    assert (personality[2], personality[3], personality[4]) == ('MEDIUMTEXT', 1, None)


def test_mysql_classification_resumes_after_column_commit(legacy_database):  # noqa: F811
    """分类列已提交但索引未建时重启补索引，不重复 ADD，不改写原归档。"""
    with sqlite3.connect(legacy_database.sqlite_path) as connection:
        connection.execute(
            "INSERT INTO agent_bot_call_record(type,source,source_id,status,request_id) "
            "VALUES ('ordinary','old','1','completed','qq-summary-history')"
        )
    connect, state = mysql_factory(legacy_database, fail_classification=True)
    params = DatabaseParams(backend="mysql", database="offline")
    with pytest.raises(UpgradeError, match="call-record-classification-v1"):
        upgrade_database(params, connect=connect, read_resource=resource)
    assert not query(legacy_database, "PRAGMA index_info(idx_call_system_time)")
    assert query(legacy_database, "SELECT request_id,system_kind FROM agent_bot_call_record") == [
        ("qq-summary-history", "command"),
    ]
    result = upgrade_database(params, connect=connect, read_resource=resource)
    assert result.applied == ("call-record-classification-v1", "persistent-agent-v1")
    indexes = query(legacy_database, "PRAGMA index_info(idx_call_system_time)")
    assert [row[2] for row in indexes] == ["system_kind", "create_time", "id"]
    assert sum("ADD COLUMN system_kind" in event for event in state.events) == 1
    assert upgrade_database(params, connect=connect, read_resource=resource).applied == ()


def test_mysql_autonomy_partial_ddl_resumes_before_version_advance(legacy_database):  # noqa: F811
    """五表迁移中途断开不报成功，重启复用已提交结构并完成索引外键核验。"""
    connect, _ = mysql_factory(legacy_database, fail_autonomy=True)
    params = DatabaseParams(backend='mysql', database='offline')
    upgrade_database(params, connect=connect, target_version='0.6.2',
                     read_resource=released_resource)
    with pytest.raises(UpgradeError, match='persistent-agent-v1'):
        upgrade_database(params, connect=connect, read_resource=resource)
    assert query(legacy_database, 'SELECT version FROM agent_bot_schema_version') == [('0.6.2',)]
    assert query(legacy_database, 'SELECT status FROM agent_bot_schema_migration '
                 "WHERE migration_id='persistent-agent-v1'") == [('failed',)]
    result = upgrade_database(params, connect=connect, read_resource=resource)
    assert result.applied == ('persistent-agent-v1',)
    assert result.current_version == '0.6.3'
    assert upgrade_database(params, connect=connect, read_resource=resource).applied == ()
