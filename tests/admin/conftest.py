"""管理接口测试复用生产配置存储与隔离数据库替身。"""

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest

from agent.conftest import agent_database as agent_database
from agent.conftest import agent_params as agent_params
from agent.conftest import seeded_model as seeded_model
from agent_bot.config import RuntimeConfig
from agent_bot.settings import DatabaseParams, SettingsStore
from media.conftest import media_database as media_database
from media.conftest import media_model as media_model


@dataclass
class FakeRuntime:
    """Supply runtime observations without starting a QQ connection."""

    config: RuntimeConfig
    revision: int = 1
    ready: bool = True
    client: object = field(default_factory=lambda: AsyncMock())
    model_guard: object = field(default_factory=lambda: AsyncMock())

    @asynccontextmanager
    async def lease(self):
        """Mirror the runtime lease boundary in an isolated test."""
        yield

    def bots(self):
        """No real bots are present in offline management tests."""
        return []

    async def is_ready(self):
        """Return controlled dependency health without network access."""
        return self.ready


@pytest.fixture
def admin_runtime():
    """Create a valid runtime whose secrets are conspicuous in disclosure assertions."""
    return FakeRuntime(
        RuntimeConfig(
            onebot_v11_access_token="offline-onebot-token-at-least-thirty-two-characters",
            dispatch_chat_session_secret="offline-session-secret-at-least-thirty-two-characters",
            dispatch_chat_ai_token="offline-ai-secret-do-not-disclose",
        )
    )


@pytest.fixture
def admin_store(fake_mysql, admin_runtime):
    """Use the production settings store against the shared Fake MySQL implementation."""
    store = SettingsStore(
        DatabaseParams("localhost", 3306, "offline", "offline", "offline"),
        connect=fake_mysql.connect,
    )
    store.initialize(admin_runtime.config)
    return store
