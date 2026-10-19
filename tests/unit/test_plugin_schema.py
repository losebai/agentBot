"""平台启动只校验已有 MySQL 表，不执行创建或升级 SQL。"""

from agent_bot.database import connect_database, initialize_database
from agent_bot.settings import DatabaseParams


def test_mysql_schema_validation_is_read_only_using_sqlite_metadata_adapter(tmp_path):
    """用真实临时表模拟 MySQL 元数据，逐条拒绝任何启动 DDL。"""
    from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
    from agent_bot.session_schema import SessionContextSchema

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "schema.sqlite3"))
    initialize_database(params)
    statements = []

    class ReadOnlyCursor:
        """记录真实查询并阻止建表、改表或删除表。"""

        def __init__(self, cursor):
            """保留真实 DB-API 行为。"""
            self.cursor = cursor

        def __enter__(self):
            """让底层游标准确进入同一上下文。"""
            self.cursor.__enter__()
            return self

        def __exit__(self, *args):
            """异常和退出由实际游标处理。"""
            return self.cursor.__exit__(*args)

        def execute(self, statement, values=None):
            """结构检查只能读，不通过默认行为创建表或修复列。"""
            statements.append(statement)
            assert statement.lstrip().split()[0].upper() not in {"CREATE", "ALTER", "DROP"}
            return self.cursor.execute(statement, values)

        def __getattr__(self, name):
            """其余读取委托给真实游标。"""
            return getattr(self.cursor, name)

    class ReadOnlyConnection:
        """只拦截 SQL 边界，数据和元数据均来自临时数据库。"""

        def __init__(self, connection):
            """保存唯一连接。"""
            self.connection = connection

        def cursor(self):
            """返回受检查的游标。"""
            return ReadOnlyCursor(self.connection.cursor())

        def __getattr__(self, name):
            """提交、回滚和关闭仍由真实连接负责。"""
            return getattr(self.connection, name)

    def connect(**kwargs):
        """使用 SQLite 的元数据适配器，不连接真实 MySQL。"""
        return ReadOnlyConnection(connect_database(params, **kwargs))

    mysql = DatabaseParams(backend="mysql", database="offline")
    SessionContextSchema(mysql, connect=connect).validate()
    TaggedMemoryStore(mysql, secret="test-session-secret" * 3, connect=connect).validate()
    assert statements and all(statement.lstrip().startswith("SELECT") for statement in statements)
