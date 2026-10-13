from __future__ import annotations

import os
from unittest.mock import AsyncMock, Mock, patch

import fakeredis.aioredis
import pytest
from nonebug import NONEBOT_INIT_KWARGS

from _fake_mysql import FakeMySQL


def pytest_configure(config: pytest.Config) -> None:
    """离线测试只使用显式配置，避免开发者 .env 改变插件生命周期。"""
    config.stash[NONEBOT_INIT_KWARGS] = {
        "_env_file": [".nonebug-no-dotenv"],
        "driver": "~fastapi",
        "command_start": {"/"},
        "onebot_v11_access_token": "test-onebot-access-token-that-is-at-least-32-characters",
        "chat_session_secret": "test-session-secret-that-is-at-least-32-characters",
        "chat_allow_all": True,
        "chat_cooldown_seconds": 0,
        "chat_auto_speak_enabled": False,
    }


@pytest.fixture(scope="session", autouse=True)
async def after_nonebot_init(after_nonebot_init: None) -> None:
    import nonebot
    from nonebot.adapters.onebot.v11 import Adapter

    driver = nonebot.get_driver()
    driver.register_adapter(Adapter)
    bootstrap_environment = {
        "CHAT_DB_BACKEND": "mysql",
        "CHAT_DB_HOST": "127.0.0.1",
        "CHAT_DB_PORT": "3306",
        "CHAT_DB_USER": "chat_test",
        "CHAT_DB_PASSWORD": "chat_test_password",
        "CHAT_DB_NAME": "chat_test",
    }
    with patch.dict(os.environ, bootstrap_environment):
        plugin = nonebot.load_plugin("agent_bot.plugin")
    assert plugin is not None
    from agent_bot.integrations.redis_queue import RedisConversationQueueStore
    from agent_bot.plugin import application as chat_plugin_module

    chat_plugin_module._memory_schema = Mock(initialize=Mock())
    chat_plugin_module._tagged_memory_store = Mock(initialize=Mock())
    fake_redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    chat_plugin_module._redis_client = fake_redis
    chat_plugin_module.conversation_queue.store = RedisConversationQueueStore(
        fake_redis,
        key_prefix="test:plugin",
        group_name="workers",
        consumer_name="consumer-1",
        max_pending=1000,
        result_ttl_seconds=60,
    )
    chat_plugin_module.image_queue.store = RedisConversationQueueStore(
        fake_redis, key_prefix="test:images", group_name="image-workers",
        consumer_name="image-consumer", max_pending=1000, result_ttl_seconds=60,
    )
    yield


@pytest.fixture
def chat_plugin(
    after_nonebot_init: None,
    monkeypatch: pytest.MonkeyPatch,
):
    from agent_bot.plugin import application as chat_plugin_module

    # 入队预检同样访问目录；普通插件测试使用显式替身，配置失败另有定向测试。
    monkeypatch.setattr(
        chat_plugin_module.chat_service, "_model_guard",
        Mock(ensure_enabled=AsyncMock()),
    )
    monkeypatch.setattr(
        chat_plugin_module,
        "_resolve_group_name",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        chat_plugin_module.conversation_queue,
        "enqueue",
        AsyncMock(return_value="1-0"),
    )
    return chat_plugin_module


@pytest.fixture
def fake_mysql(monkeypatch: pytest.MonkeyPatch) -> FakeMySQL:
    """提供共享内存态的 fake，并替换 settings 模块中的 pymysql.connect。"""

    import agent_bot.settings as settings_module

    fake = FakeMySQL()
    monkeypatch.setattr(settings_module.pymysql, "connect", fake.connect)
    return fake
