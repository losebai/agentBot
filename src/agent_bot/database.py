"""按明确后端提供统一连接和离线初始化，不改变现有事务所有者。"""

from __future__ import annotations

from importlib.metadata import distribution
from typing import TYPE_CHECKING, Any

import pymysql

if TYPE_CHECKING:
    from agent_bot.settings import DatabaseParams


def connect_database(params: DatabaseParams, **kwargs: Any) -> Any:
    """只连接选定数据库，失败不会创建 SQLite 文件或回退其他后端。"""
    if params.backend == "sqlite":
        from agent_bot.sqlite_database import SqliteConnection

        return SqliteConnection(
            params.sqlite_path,
            timeout=float(kwargs.get("read_timeout", kwargs.get("connect_timeout", 10))),
            cursorclass=kwargs.get("cursorclass", pymysql.cursors.Cursor),
            autocommit=bool(kwargs.get("autocommit", False)),
        )
    options = {
        "host": params.host,
        "port": params.port,
        "user": params.user,
        "password": params.password,
        "database": params.database,
        "charset": "utf8mb4",
    }
    return pymysql.connect(**(options | kwargs))


def database_connector(params: DatabaseParams):
    """绑定唯一数据库，让原有存储保持显式注入连接的调用形式。"""

    def connect(**kwargs: Any) -> Any:
        """将游标和超时参数交给所选后端。"""
        return connect_database(params, **kwargs)

    return connect


def initialize_database(params: DatabaseParams) -> None:
    """显式创建全部业务表，重复执行保留已有数据，不插入配置或凭据。"""
    if params.backend == "sqlite":
        from agent_bot.sqlite_database import initialize_sqlite

        initialize_sqlite(params.sqlite_path)
        return
    resource = distribution("taobot").locate_file("agent_bot/resources/agent_bot_init.sql")
    sql = resource.read_text(encoding="utf-8")
    connection = connect_database(params)
    try:
        with connection.cursor() as cursor:
            for statement in sql.split(";"):
                if statement.strip():
                    cursor.execute(statement)
        connection.commit()
    finally:
        connection.close()
