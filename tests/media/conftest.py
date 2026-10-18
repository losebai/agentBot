"""媒体测试使用本地事务替身、可控时钟和临时文件根。"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from agent.conftest import agent_database, agent_params, seeded_model  # noqa: F401
from agent_bot.ai.contracts import ModelMediaCapabilities, ModelUpdate


class FakeClock:
    """在不等待真实时间的情况下验证过期与清理。"""

    def __init__(self):
        """固定初始协调世界时。"""
        self.now = datetime(2026, 10, 12)

    def __call__(self):
        """返回可持久化的无时区协调世界时。"""
        return self.now

    def advance(self, seconds):
        """推进时钟以触发租约或过期边界。"""
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def media_database(agent_database):  # noqa: F811
    """复用既有数据库替身；SQL 中文说明另外核对 MySQL 脚本。"""
    agent_database.connection.executescript("""
    CREATE TABLE agent_bot_ai_model_media(model_id INTEGER PRIMARY KEY
        REFERENCES agent_bot_ai_model(id) ON DELETE CASCADE,
        vision_enabled INTEGER NOT NULL DEFAULT 0, image_api_profile TEXT NOT NULL DEFAULT 'none',
        audio_chat_enabled INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE agent_bot_media_run(execution_id TEXT PRIMARY KEY, entry TEXT, session_id TEXT,
        request_id TEXT, model TEXT, backend TEXT, output_type TEXT, status TEXT,
        duration_ms INTEGER, prompt TEXT, content TEXT, error_code TEXT, created_at TEXT);
    CREATE TABLE agent_bot_media_asset(id TEXT PRIMARY KEY, entry TEXT, session_id TEXT,
        execution_id TEXT, execution_kind TEXT, purpose TEXT, mime_type TEXT, size_bytes INTEGER,
        sha256 TEXT, storage_key TEXT, status TEXT, created_at TEXT, expires_at TEXT);
    """)
    agent_database.connection.executescript(
        Path("sql/2026-10-17-material-memory.sqlite.sql").read_text(encoding="utf-8")
    )
    return agent_database


@pytest.fixture
def media_clock():
    """每个测试独立控制清理时钟。"""
    return FakeClock()


@pytest.fixture
async def media_model(media_database, seeded_model):  # noqa: F811
    """显式启用全部媒体能力，避免通过模型名称推断。"""
    catalog, provider, model = seeded_model
    model = await catalog.update_model(
        model.id,
        ModelUpdate(
            expected_revision=model.revision,
            media=ModelMediaCapabilities(
                vision_enabled=True, image_api_profile="gpt-image", audio_chat_enabled=True
            ),
        ),
    )
    return catalog, provider, model
