"""托管模式仅由原生菜单修改，手工 HTTP 草稿也不能越过权限。"""

import pytest


def test_napcat_fields_are_native_only(admin_store, admin_runtime, tmp_path):
    """即使注入具有全部字段写权限的文件 store，管理服务仍拒绝托管字段。"""
    from agent_bot.admin.models import STARTUP_SETTING_NAMES, SettingsUpdate
    from agent_bot.admin.settings import SettingsService
    from agent_bot.settings import SettingsValidationError
    from agent_bot.startup_settings import StartupSettingsStore

    env = tmp_path / ".env"
    env.write_text(
        "ONEBOT_V11_ACCESS_TOKEN=offline-onebot-token-at-least-thirty-two-characters\n"
        "CHAT_SESSION_SECRET=offline-session-secret-at-least-thirty-two-characters\n",
        encoding="utf-8",
    )
    startup = StartupSettingsStore(tmp_path, STARTUP_SETTING_NAMES)
    service = SettingsService(admin_store, admin_runtime, startup_settings_store=startup)
    snapshot = service.read()
    fields = {item.name: item for item in snapshot.fields}
    assert fields["agent_bot_napcat_mode"].read_only
    original = env.read_bytes()
    with pytest.raises(SettingsValidationError):
        service.update(SettingsUpdate(
            expected_revision=1, startup_revision=snapshot.startup_revision,
            values={"agent_bot_napcat_mode": "managed"}))
    assert env.read_bytes() == original
    assert admin_store.load().revision == 1
