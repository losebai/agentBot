"""真实文件数据库验证初始化、事务及 DB-API 消费边界。"""

from contextlib import closing
from datetime import datetime
from pathlib import Path

import pymysql
import pytest

from agent_bot.settings import DatabaseParams


def sqlite_params(tmp_path: Path) -> DatabaseParams:
    """每个测试使用独立文件，禁止连接开发者数据库。"""
    return DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.sqlite3"))


def test_initialize_all_tables_and_preserve_existing_data(tmp_path: Path) -> None:
    """重复初始化不能清空已保存供应商。"""
    from agent_bot.database import connect_database, initialize_database

    params = sqlite_params(tmp_path)
    initialize_database(params)
    with closing(connect_database(params, cursorclass=pymysql.cursors.DictCursor)) as db:
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name<>'sqlite_sequence'"
            )
            tables = {row["name"] for row in cursor.fetchall()}
            assert {"chat_knowledge", "chat_knowledge_tag", "agent_bot_ai_provider"} <= tables
            cursor.execute(
                "INSERT INTO agent_bot_ai_provider (name,protocol,base_url,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    "本地",
                    "openai-chat",
                    "http://localhost:1234/v1",
                    datetime(2026, 10, 13),
                    datetime(2026, 10, 13),
                ),
            )
        db.commit()
    initialize_database(params)
    with closing(connect_database(params, cursorclass=pymysql.cursors.DictCursor)) as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT name FROM agent_bot_ai_provider")
            assert cursor.fetchall() == [{"name": "本地"}]


def test_rollback_datetime_and_dictionary_cursor(tmp_path: Path) -> None:
    """提交与回滚影响真实跨连接数据，时间保留微秒。"""
    from agent_bot.database import connect_database, initialize_database

    params = sqlite_params(tmp_path)
    initialize_database(params)
    now = datetime(2026, 10, 13, 1, 2, 3, 456789)
    with closing(connect_database(params, cursorclass=pymysql.cursors.DictCursor)) as db:
        with db.cursor() as cursor:
            cursor.execute("INSERT INTO chat_memory_receipt VALUES (%s,%s,%s)", ("s", "r", now))
        db.rollback()
        with db.cursor() as cursor:
            cursor.execute("SELECT * FROM chat_memory_receipt")
            assert cursor.fetchall() == []
            cursor.execute("INSERT INTO chat_memory_receipt VALUES (%s,%s,%s)", ("s", "r", now))
        db.commit()
    with closing(connect_database(params, cursorclass=pymysql.cursors.DictCursor)) as db:
        with db.cursor() as cursor:
            cursor.execute("SELECT expires_at FROM chat_memory_receipt")
            assert cursor.fetchone() == {"expires_at": now}


def test_unique_conflict_rolls_back_and_foreign_keys_are_enforced(tmp_path: Path) -> None:
    """适配错误保持现有业务异常边界，不能跳过约束。"""
    from agent_bot.database import connect_database, initialize_database

    params = sqlite_params(tmp_path)
    initialize_database(params)
    with closing(connect_database(params)) as db:
        with db.cursor() as cursor:
            with pytest.raises(pymysql.IntegrityError):
                cursor.execute(
                    "INSERT INTO agent_bot_agent_skill (agent_id,skill_id) VALUES (%s,%s)",
                    (999, 999),
                )
        db.rollback()


def test_missing_file_does_not_create_or_switch_database(tmp_path: Path) -> None:
    """普通连接必须先显式初始化，错误不可创建空文件或切换 MySQL。"""
    from agent_bot.database import connect_database

    params = sqlite_params(tmp_path)
    with pytest.raises(pymysql.OperationalError):
        connect_database(params)
    assert not (tmp_path / "bot.sqlite3").exists()


def test_json_columns_reject_invalid_payloads(tmp_path: Path) -> None:
    """SQLite 保留 MySQL JSON 字段约束，不允许无效 JSON 进入业务表。"""
    from agent_bot.database import connect_database, initialize_database

    params = sqlite_params(tmp_path)
    initialize_database(params)
    with closing(connect_database(params)) as db:
        with db.cursor() as cursor:
            with pytest.raises(pymysql.IntegrityError):
                cursor.execute(
                    "INSERT INTO agent_bot_skill (name,description,category,system_prompt,"
                    "sample_prompt,tools,created_at,updated_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        "测试",
                        "用途",
                        "general",
                        "提示",
                        "示例",
                        "invalid-json",
                        datetime(2026, 10, 13),
                        datetime(2026, 10, 13),
                    ),
                )


def test_sqlite_pool_returns_connections_and_closes(tmp_path: Path) -> None:
    """连续请求和关闭后访问必须遵守同一有限资源边界。"""
    from agent_bot.database import initialize_database
    from agent_bot.database_pool import create_database_pool

    params = sqlite_params(tmp_path)
    initialize_database(params)
    pool = create_database_pool(params, max_size=1)
    for _ in range(3):
        with closing(pool.connect(cursorclass=pymysql.cursors.DictCursor)) as db:
            with db.cursor() as cursor:
                cursor.execute("SELECT 1 AS ok")
                assert cursor.fetchone() == {"ok": 1}
    pool.close()
    with pytest.raises(pymysql.InterfaceError):
        pool.connect()
