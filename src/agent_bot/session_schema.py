"""校验统一会话上下文结构，普通启动不创建或迁移部署数据库。"""

from collections.abc import Callable
from contextlib import closing
from typing import Any

import pymysql

from agent_bot.database import database_connector
from agent_bot.memory_store import ChatMemorySchemaError
from agent_bot.settings import DatabaseParams


class SessionContextSchema:
    """只依赖当前上下文和可选群背景结构，不要求旧轮次或出站表。"""

    def __init__(
        self, params: DatabaseParams, *, connect: Callable[..., Any] | None = None,
    ) -> None:
        """绑定明确选择的后端，构造期间不连接或初始化数据库。"""
        self.params = params
        self.connect = connect or database_connector(params)

    def validate(self) -> None:
        """用空结果查询验证运行所需字段，缺结构时要求显式迁移。"""
        try:
            with closing(self._open()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT session_id,generation_id,messages_json,memory_summary,"
                        "memory_revision,deliveries_json,create_time,update_time "
                        "FROM chat_session_context LIMIT 0"
                    )
                    cursor.execute(
                        "SELECT scope_id,messages_json,expires_at FROM chat_group_context LIMIT 0"
                    )
                    cursor.execute(
                        "SELECT COLUMN_NAME FROM information_schema.statistics "
                        "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s AND INDEX_NAME=%s "
                        "ORDER BY SEQ_IN_INDEX",
                        (self.params.database, "chat_session_context", "PRIMARY"),
                    )
                    if [row["COLUMN_NAME"] for row in cursor.fetchall()] != ["session_id"]:
                        raise ChatMemorySchemaError("统一会话必须以 session_id 为唯一主键")
                connection.rollback()
        except pymysql.DatabaseError as error:
            raise ChatMemorySchemaError(
                "统一会话上下文未初始化，请先执行 session-context 增量脚本和历史迁移"
            ) from error

    def _open(self) -> Any:
        """复用项目连接端口及超时，检查不会访问其他数据库后端。"""
        return self.connect(
            host=self.params.host, port=self.params.port, user=self.params.user,
            password=self.params.password, database=self.params.database,
            charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
            autocommit=False, connect_timeout=5, read_timeout=10, write_timeout=10,
        )
