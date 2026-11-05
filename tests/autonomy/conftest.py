"""持续智能体测试仅使用临时SQLite和显式配置，不启动NoneBot或外部依赖。"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from agent_bot.autonomy.contracts import Identity, Policy, ProfileCreate
from agent_bot.autonomy.service import AutonomyService
from agent_bot.autonomy.store import AutonomyStore
from agent_bot.database import database_connector
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.jobs.contracts import JobOwner
from agent_bot.settings import DatabaseParams

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def _nonebot_init():
    """纯领域测试不初始化平台适配器。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def after_nonebot_init():
    """屏蔽全局插件夹具，避免加载开发者本机配置。"""
    yield


@pytest.fixture(scope="session", autouse=True)
def nonebug_init():
    """所有网络和模型均需由测试显式替身提供。"""
    yield


@pytest.fixture
def clock():
    """测试时间可推进，初始北京时间中午，默认不在静默时段。"""
    return [datetime(2026, 11, 4, 4, tzinfo=timezone.utc)]


@pytest.fixture
def store(tmp_path, clock):
    """完整初始化结构与生产连接器验证SQLite约束及事务行为。"""
    import sqlite3

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "autonomy.sqlite3"))
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.executescript((ROOT / "sql/agent_bot_init.sqlite.sql").read_text("utf-8"))
    return AutonomyStore(
        params,
        connect=database_connector(params),
        secret="offline-autonomy-secret",
        clock=lambda: clock[0],
    )


@pytest.fixture
def memories(store):
    """事实层使用同一个隔离文件和身份密钥，不访问现网记忆。"""
    return TaggedMemoryStore(store.params, connect=store.connect, secret="offline-autonomy-secret")


@pytest.fixture
def service(store, memories):
    """默认无模型，任何意外的模型动作必须明确失败。"""
    return AutonomyService(store, memories)


def profile_body(**policy):
    """固定所有者与身份，模型预算必须由测试逐项显式放行。"""
    return ProfileCreate(
        owner=JobOwner(
            bot_id="10001", sender_id="10002", session_id="offline-session", scene="private"
        ),
        identity=Identity(
            name="阿澄", role="记忆助手", style="简洁", boundaries="不得执行系统命令"
        ),
        policy=Policy(**policy),
    )
