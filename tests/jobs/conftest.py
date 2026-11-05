"""以真实 SQLite 事务隔离 MySQL 连接，时间由测试推进。"""

from datetime import datetime, timezone
from pathlib import Path

import pytest
from tests.ai.fake_database import Connection, Cursor, FakeDatabase

from agent_bot.jobs.store import JobStore
from agent_bot.settings import DatabaseParams


class JobCursor(Cursor):
    """把唯一一处 MySQL 所有者 upsert 映射为等价 SQLite 冲突更新。"""

    def execute(self, sql, parameters=()):
        """其他查询仍由原 DB-API 替身执行真实约束与事务。"""
        sql = sql.replace(
            "ON DUPLICATE KEY UPDATE owner_key=VALUES(owner_key)",
            "ON CONFLICT(owner_key) DO UPDATE SET owner_key=excluded.owner_key",
        )
        return super().execute(sql, parameters)


class JobConnection(Connection):
    """为任务测试使用有限 SQL 方言适配器。"""

    def cursor(self):
        """连接仍由原互斥锁保护，提交和回滚行为不变。"""
        return JobCursor(self.db.connection)


@pytest.fixture(scope="session", autouse=True)
def _nonebot_init():
    """任务领域测试不启动 NoneBot，避免读取开发者的本地 .env。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def after_nonebot_init():
    """领域测试只使用显式夹具；实际插件接入另由集成测试验证。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def nonebug_init():
    """同时隔离 NoneBug 的插件生命周期，领域测试没有后台消费者。"""
    yield


@pytest.fixture
def job_database():
    """新表只建在内存中，不执行项目的生产 DDL。"""
    database = FakeDatabase()
    database.connect = lambda **kwargs: JobConnection(database)
    database.connection.executescript("""
        CREATE TABLE agent_bot_job_owner (owner_key TEXT PRIMARY KEY);
        CREATE TABLE agent_bot_job (
            id TEXT PRIMARY KEY, owner_key TEXT, deduplication_key TEXT UNIQUE,
            bot_id TEXT, session_id TEXT, sender_id TEXT, scene TEXT, group_id TEXT,
            model TEXT, system_prompt TEXT, prompt TEXT, status TEXT,
            result TEXT, error_code TEXT, run_at TEXT, created_at TEXT,
            started_at TEXT, completed_at TEXT, delivered_at TEXT);
        CREATE TABLE agent_bot_auto_speak_decision (
            id TEXT PRIMARY KEY, session_id TEXT, request_id TEXT, mcp_code TEXT,
            mcp_revision INTEGER, status TEXT, should_speak INTEGER,
            confidence REAL, duration_ms INTEGER, error_code TEXT, created_at TEXT);
    """)
    database.connection.executescript(
        (Path(__file__).resolve().parents[2] / "sql/job-recovery.sqlite.sql").read_text("utf-8")
    )
    yield database
    database.close()


@pytest.fixture
def job_clock():
    """可推进的 UTC 时钟让测试无需真实等待。"""
    return [datetime(2026, 10, 12, 12, tzinfo=timezone.utc)]


@pytest.fixture
def job_store(job_database, job_clock):
    """使用生产 Store 及内存 DB-API 替身。"""
    return JobStore(
        DatabaseParams("localhost", 3306, "offline", "offline", "offline"),
        connect=job_database.connect,
        clock=lambda: job_clock[0],
    )
