"""纯 Python 配置下管理入口可访问，后台开关与中文配置可见。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin


async def test_python_status_settings_and_text_probe(admin_store, admin_runtime):
    """使用真实 ASGI 路由及配置存储，只替换外部模型调用。"""
    admin_runtime.ai_service = SimpleNamespace(
        readiness=AsyncMock(return_value=None),
        complete=AsyncMock(
            return_value=SimpleNamespace(content="Python 回复", metadata=lambda: None)
        ),
    )
    app = FastAPI()
    token = "offline-admin-token-longer-than-thirty-two-characters"
    install_admin(app, admin_store, admin_runtime, AdminConfig(token=token))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://offline.test",
        headers={"Authorization": f"Bearer {token}"},
    ) as api:
        status = await api.get("/api/v1/status")
        assert status.status_code == 200
        assert status.json()["plain_ai_backend"] == "python"
        assert "job" not in status.json()["legacy_capabilities"]
        assert status.json()["job_enabled"] is False
        assert status.json()["auto_speak_enabled"] is False
        settings = await api.get("/api/v1/settings")
        fields = {field["name"]: field for field in settings.json()["fields"]}
        assert "chat_job_enabled" in fields and "chat_auto_speak_mcp_code" in fields
        assert fields["chat_auto_speak_bot_id"]["kind"] == "secret"
        result = await api.post("/api/v1/chat", json={"prompt": "测试", "session_id": "s"})
        assert result.status_code == 200 and result.json()["content"] == "Python 回复"
        admin_runtime.ai_service.complete.assert_awaited_once()
        assert (await api.get("/health/ready", headers={"Authorization": ""})).status_code == 200
        admin_runtime.ready = False
        readiness = await api.get("/health/ready", headers={"Authorization": ""})
        assert readiness.status_code == 503 and readiness.json() == {"status": "unavailable"}
