"""独立进程验证真实宿主装配，禁止真实 QQ、Redis 和付费模型访问。"""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("enabled", [False, True], ids=["without-qq", "with-qq"])
def test_application_lifecycle_uses_one_core_and_only_enabled_channel(tmp_path, enabled):
    """使用临时 SQLite 和无外联 OneBot 验证启停、快照共享和退出。"""
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("CHAT_", "ONEBOT_", "AGENT_BOT_"))}
    environment.update({
        "CHAT_DB_BACKEND": "sqlite", "CHAT_DB_SQLITE_PATH": str(tmp_path / "bot.sqlite3"),
        "CHAT_QUEUE_BACKEND": "sqlite", "CHAT_QUEUE_SQLITE_PATH": str(tmp_path / "queue.sqlite3"),
        "AGENT_BOT_MEDIA_DIRECTORY": str(tmp_path / "media"),
    })
    code = '''
import asyncio
import sys
from pathlib import Path
import httpx
import nonebot
from agent_bot.admin.config import AdminConfig
from agent_bot.application import create_application
from agent_bot.config import RuntimeConfig
from agent_bot.database import initialize_database
from agent_bot.settings import DatabaseParams, SettingsStore

enabled = sys.argv[1] == "yes"
config = RuntimeConfig(driver="~fastapi", app_enabled_plugins=["qqbot"] if enabled else [],
                       onebot_v11_access_token="offline-onebot-" + "o" * 32,
                       chat_session_secret="offline-session-" + "s" * 32)
Path(".env").write_text("DRIVER=~fastapi\\nONEBOT_V11_ACCESS_TOKEN="
                       + config.onebot_v11_access_token.get_secret_value()
                       + "\\nCHAT_SESSION_SECRET="
                       + config.chat_session_secret.get_secret_value() + "\\n", encoding="utf-8")
params = DatabaseParams(backend="sqlite", sqlite_path="bot.sqlite3")
initialize_database(params)
stored = SettingsStore(params, static_config=config).initialize(config)
app = create_application(stored, AdminConfig(token=""))

async def check():
    runtime, plugins = app.state.runtime, app.state.plugins
    assert not Path("queue.sqlite3").exists()
    assert not enabled or "agent_bot.plugin.application" in sys.modules
    entered = asyncio.Event()
    early = None
    if enabled:
        from agent_bot.plugin import application as qq
        async def incoming_during_startup():
            from types import SimpleNamespace
            await qq._is_authorized(SimpleNamespace(user_id=10001))
            async with qq._runtime_lease():
                entered.set()
                assert qq.get_runtime_state() is not None
        early = asyncio.create_task(incoming_during_startup())
        await asyncio.sleep(0)
        assert not entered.is_set(), "QQ event entered before shared runtime and queue startup"
    async with app.router.lifespan_context(app):
        if early is not None:
            await asyncio.wait_for(early, timeout=2)
        assert await runtime.is_ready() and await plugins.is_ready()
        assert runtime.state.ai_client.agent_service is not None
        assert runtime.state.ai_client.media_service is not None
        if enabled:
            from agent_bot.plugin import application as qq
            assert qq.get_runtime_state().ai_client is runtime.state.ai_client
            assert not qq.get_runtime_state().owns_core
            assert qq._database_pool is runtime.database_pool
            assert qq._settings_refresh_task is None and qq._ai_job_runner_task is None
            assert qq.conversation_queue.running and qq.image_queue.running
        else:
            assert "agent_bot.plugin.application" not in sys.modules
            assert not Path("queue.sqlite3").exists()
            assert not nonebot.get_driver()._adapters
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://offline.test") as client:
            assert (await client.get("/health/ready")).status_code == 200
            info = (await client.get("/api/v1/plugins")).json()[0]
            assert info["enabled"] == enabled
            assert info["status"] == ("running" if enabled else "disabled")
            assert (await client.get("/api/v1/bots")).json() == []
            response = await client.patch("/api/v1/settings", json={
                "expected_revision": stored.revision,
                "values": {"app_enabled_plugins": [] if enabled else ["qqbot"],
                           "chat_system_prompt": "offline new prompt"}})
            assert response.status_code == 200, response.text
            await runtime.refresh_once()
            assert runtime.config.app_enabled_plugins == config.app_enabled_plugins
            assert runtime.config.chat_system_prompt == "offline new prompt"
            assert (await client.get("/api/v1/plugins")).json()[0]["restart_required"]
            if enabled:
                assert qq.get_runtime_state().ai_client is runtime.state.ai_client
                assert qq.conversation_queue.running
    assert not await runtime.is_ready()
    assert runtime._refresh_task is None and runtime._job_task is None

asyncio.run(check())
'''
    result = subprocess.run([sys.executable, "-c", code, "yes" if enabled else "no"],
                            cwd=tmp_path, env=environment, capture_output=True, text=True,
                            encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
