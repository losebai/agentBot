"""管理校验失败指出具体字段与规则，不能回显提交内容或异常原文。"""

import pytest
from pydantic import ValidationError

from admin.test_routes import HEADERS
from admin.test_routes import admin_app as admin_app
from admin.test_routes import api as api
from agent_bot.config import RuntimeConfig
from agent_bot.settings import SettingsValidationError


@pytest.mark.parametrize(("values", "field", "reason"), [
    ({"chat_max_concurrent_requests": 0}, "chat_max_concurrent_requests", "必须大于 0"),
    ({"chat_auto_speak_enabled": "private-not-a-bool"}, "chat_auto_speak_enabled", "布尔值"),
    ({"chat_chunk_chars": 9000}, "chat_chunk_chars", "不能超过"),
    ({"chat_auto_speak_groups": ["private-not-a-qq"]}, "chat_auto_speak_groups", "QQ"),
    ({"chat_tts_token": "***"}, "chat_tts_token", "掩码"),
])
async def test_settings_validation_identifies_field_without_saving(
    api, admin_store, values, field, reason,
) -> None:
    """请求类型、完整配置范围及组合校验都必须指出字段且拒绝保存。"""
    response = await api.patch("/api/v1/settings", headers=HEADERS, json={
        "expected_revision": 1, "values": values,
    })
    assert response.status_code == 422
    assert response.json()["code"] == "INVALID_REQUEST"
    assert field in response.json()["message"]
    assert reason in response.json()["message"]
    assert "private-" not in response.text
    assert admin_store.load().revision == 1


async def test_auto_speak_dependency_validation_identifies_missing_model(api) -> None:
    """自动发言依赖的模型缺失不能只显示配置组合失败。"""
    response = await api.patch("/api/v1/settings", headers=HEADERS, json={
        "expected_revision": 1,
        "values": {
            "chat_auto_speak_enabled": True, "chat_auto_speak_bot_id": "90001",
            "chat_auto_speak_groups": ["20001"], "chat_auto_speak_trigger_mode": "message",
            "chat_auto_speak_decision_backend": "llm", "chat_auto_speak_decision_model": "",
            "chat_group_recent_context_enabled": True,
        },
    })
    assert response.status_code == 422
    message = response.json()["message"]
    assert "chat_auto_speak_decision_model" in message and "独立" in message


async def test_enabling_auto_speak_with_defaults_identifies_missing_identity(api) -> None:
    """直接开启全新自动发言配置时，公开缺失的机器人 QQ 与目标群字段。"""
    response = await api.patch("/api/v1/settings", headers=HEADERS, json={
        "expected_revision": 1, "values": {"chat_auto_speak_enabled": True},
    })
    assert response.status_code == 422
    message = response.json()["message"]
    assert "chat_auto_speak_bot_id" in message
    assert "chat_auto_speak_groups" in message


async def test_agent_history_validation_identifies_relevant_fields(api) -> None:
    """Agent 根级校验的历史预算错误应指出提示词、历史和附图字段。"""
    response = await api.post("/api/v1/agent-runs", headers=HEADERS, json={
        "agent_code": "main", "prompt": " ", "session_id": "web-test",
    })
    assert response.status_code == 422
    assert "prompt" in response.json()["message"]


async def test_request_validation_lists_nested_fields_and_missing_values(api) -> None:
    """嵌套输入保留已知字段和列表索引，不回显无效正文或正则原文。"""
    response = await api.post("/api/v1/chat", headers=HEADERS, json={
        "session_id": "private/invalid-session",
        "messages": [{"role": "user", "content": 123}],
    })
    assert response.status_code == 422
    message = response.json()["message"]
    assert "prompt" in message and "必填" in message
    assert "session_id" in message
    assert "messages[0].content" in message
    assert "private/invalid-session" not in response.text


async def test_unknown_field_names_and_custom_error_text_are_not_disclosed(
    api, admin_store, monkeypatch,
) -> None:
    """额外字段名和校验异常可能含秘密，只公开服务端已声明的字段名。"""
    response = await api.post("/api/v1/chat", headers=HEADERS, json={
        "prompt": "hello", "session_id": "web-test", "private-secret-field": "private-value",
    })
    assert response.status_code == 422
    assert "未知字段" in response.json()["message"]
    assert "private" not in response.text

    def invalid_snapshot():
        """模拟配置仓库包装真实 Pydantic 错误，外层错误文本不得泄露。"""
        try:
            RuntimeConfig.model_validate({"chat_max_concurrent_requests": 0})
        except ValidationError as error:
            raise SettingsValidationError("private-store-error") from error

    monkeypatch.setattr(admin_store, "load", invalid_snapshot)
    response = await api.get("/api/v1/settings", headers=HEADERS)
    assert response.status_code == 422
    assert "chat_max_concurrent_requests" in response.json()["message"]
    assert "private-store-error" not in response.text


async def test_readonly_startup_validation_identifies_submitted_field(api) -> None:
    """启动源只读时指出被拒绝字段，不回显启动配置值。"""
    response = await api.patch("/api/v1/settings", headers=HEADERS, json={
        "expected_revision": 1, "startup_revision": "0" * 64,
        "values": {"chat_db_host": "private-database-host"},
    })
    assert response.status_code == 422
    assert "chat_db_host" in response.json()["message"]
    assert "只读" in response.json()["message"]
    assert "private-database-host" not in response.text
