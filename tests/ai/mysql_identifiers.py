"""仅为离线回归补充 SQLite 无法覆盖的 MySQL 保留字语法限制。"""

import re

import pymysql

from .fake_database import Cursor


def reject_unquoted_usage(monkeypatch):
    """保留真实事务和约束，仅在游标边界拒绝未引用的 usage 列名。"""
    execute = Cursor.execute

    def execute_mysql(self, sql, parameters=()):
        """模拟已确认的 MySQL 1064，避免 permissive SQLite 掩盖协议差异。"""
        # JSON 键和路径属于字符串常量，不能按列名保留字误报。
        identifiers = re.sub(r"'(?:''|[^'])*'", "''", sql)
        if re.search(r"(?<![\w`])usage(?![\w`])", identifiers, re.IGNORECASE):
            raise pymysql.ProgrammingError(1064, "unquoted reserved identifier: usage")
        return execute(self, sql, parameters)

    monkeypatch.setattr(Cursor, "execute", execute_mysql)
