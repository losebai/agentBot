"""统一会话表的新库和增量结构一致，启动校验拒绝缺少主键的错误部署。"""

import re
import sqlite3
from pathlib import Path

import pytest

from agent_bot.memory_store import ChatMemorySchemaError
from agent_bot.session_schema import SessionContextSchema
from agent_bot.settings import DatabaseParams


@pytest.fixture(scope="session", autouse=True)
def after_nonebot_init():
    """结构测试不启动 QQ 插件。"""


def _create_statement(path: str, table: str) -> str:
    """从已发布脚本提取指定建表语句，不手写另一套测试结构。"""
    source = re.sub(r"--[^\n]*", "", Path(path).read_text("utf-8"))
    found = re.search(
        rf"CREATE TABLE (?:IF NOT EXISTS )?{re.escape(table)}\s*\(.*?\)\s*(?:ENGINE[^;]*)?;",
        source,
        re.S,
    )
    assert found is not None, f"脚本缺少{table}定义"
    return found.group()


def _sqlite_structure(connection):
    """忽略ALTER造成的列物理位置差异，完整保留有效类型、默认、可空和约束。"""
    columns = {
        row[1]: row[2:] for row in connection.execute("PRAGMA table_info(chat_session_context)")
    }
    indexes = {
        row[1]: (
            row[2],
            row[3],
            row[4],
            [(item[0], item[2]) for item in connection.execute(f'PRAGMA index_info("{row[1]}")')],
        )
        for row in connection.execute("PRAGMA index_list(chat_session_context)")
    }
    foreign_keys = connection.execute("PRAGMA foreign_key_list(chat_session_context)").fetchall()
    return columns, indexes, foreign_keys


def _assert_sqlite_context_constraints(connection):
    """真实插入核验主键、非空及新增JSON约束，避免仅列名相同就判断一致。"""
    values = {
        "session_id": "valid-session",
        "generation_id": "a" * 32,
        "messages_json": "[]",
        "memory_summary": "",
        "deliveries_json": "{}",
        "create_time": "2026-10-20 00:00:00",
        "update_time": "2026-10-20 00:00:00",
        "memory_facts_json": '{"schema_version":1,"entries":[]}',
    }
    sql = (
        "INSERT INTO chat_session_context ("
        + ",".join(values)
        + ") VALUES ("
        + ",".join("?" for _ in values)
        + ")"
    )
    connection.execute(sql, tuple(values.values()))
    assert connection.execute("SELECT memory_revision FROM chat_session_context").fetchone() == (0,)
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(sql, tuple(values.values()))
    for changes in (
        {"session_id": "null-generation", "generation_id": None},
        {"session_id": "invalid-json", "memory_facts_json": "broken-json"},
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(sql, tuple((values | changes).values()))
    connection.execute("UPDATE chat_session_context SET memory_facts_json=NULL")
    assert connection.execute("SELECT memory_facts_json FROM chat_session_context").fetchone() == (
        None,
    )


def test_new_and_upgraded_sqlite_context_schemas_match(tmp_path):
    """真实执行历史及最新增量，比较有效结构并核对JSON、非空和主键约束。"""
    schemas = []
    for name, scripts in (
        ("fresh", ["sql/agent_bot_init.sqlite.sql"]),
        (
            "upgrade",
            [
                "sql/legacy/session-context-v1.sqlite.sql",
                "sql/2026-10-19-session-context.sqlite.sql",
                "sql/2026-10-20-memory-lifecycle.sqlite.sql",
            ],
        ),
    ):
        with sqlite3.connect(tmp_path / f"{name}.db") as connection:
            if name == "upgrade":
                # 生命周期增量还升级事实表；复用合表迁移的真实最终DDL准备其依赖。
                connection.execute(
                    _create_statement(
                        "sql/2026-10-20-merge-tagged-memory.sqlite.sql",
                        "chat_tagged_memory_merged",
                    ).replace("chat_tagged_memory_merged", "chat_tagged_memory", 1)
                )
            for script in scripts:
                connection.executescript(Path(script).read_text("utf-8"))
            schemas.append(_sqlite_structure(connection))
            _assert_sqlite_context_constraints(connection)
    assert schemas[0] == schemas[1]


def test_new_and_upgraded_mysql_context_declarations_match():
    """核对MySQL完整有效声明，允许新增字段顺序不同但保留类型、排序规则与主键。"""

    def declarations(statement):
        """只忽略注释和标点旁空白，不忽略可空性、默认值及排序规则。"""
        statement = re.sub(r"COMMENT\s+'(?:''|[^'])*'", "", statement)
        body = statement[statement.index("(") + 1 : statement.rindex(")")]
        return {
            item.strip().split()[0]: " ".join(item.split())
            for item in re.split(r",(?![^()]*\))", body)
        }

    fresh_statement = _create_statement("sql/agent_bot_init.sql", "chat_session_context")
    upgraded_statement = _create_statement(
        "sql/2026-10-19-session-context.sql", "chat_session_context"
    )
    fresh = declarations(fresh_statement)
    upgraded = declarations(upgraded_statement)
    latest = re.sub(r"--[^\n]*", "", Path("sql/2026-10-20-memory-lifecycle.sql").read_text("utf-8"))
    alteration = re.search(r"ALTER TABLE chat_session_context\s+ADD COLUMN (.*?);", latest, re.S)
    assert alteration is not None
    for item in re.split(r",\s*ADD COLUMN\s+", alteration[1]):
        item = re.sub(r"COMMENT\s+'(?:''|[^'])*'", "", item)
        column = item.strip().split()[0]
        assert column not in upgraded
        upgraded[column] = " ".join(item.split())
    assert fresh == upgraded
    # 文本列默认排序规则来自表定义，不能只比较字段而遗漏字符集或引擎差异。
    table_options = []
    for statement in (fresh_statement, upgraded_statement):
        tail = statement[statement.rindex(")") + 1 :]
        tail = re.sub(r"COMMENT\s*=\s*'(?:''|[^'])*'", "", tail)
        table_options.append(" ".join(tail.split()))
    assert table_options[0] == table_options[1]


def test_context_schema_requires_session_primary_key(tmp_path):
    """只有字段但缺少会话唯一性不能安全合并并发消息，启动必须明确拒绝。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bad.db"))
    with sqlite3.connect(params.sqlite_path) as connection:
        original = Path("sql/agent_bot_init.sqlite.sql").read_text("utf-8")
        connection.executescript(
            original.replace("session_id TEXT NOT NULL PRIMARY KEY,", "session_id TEXT NOT NULL,")
        )
    with pytest.raises(ChatMemorySchemaError, match="session_id"):
        SessionContextSchema(params).validate()
