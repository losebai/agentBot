"""插件选择的配置边界与持久化兼容测试。"""

import pytest
from pydantic import ValidationError

from agent_bot.config import RuntimeConfig
from agent_bot.runtime_fields import RESTART_REQUIRED_FIELDS
from agent_bot.static_config import DYNAMIC_SETTING_NAMES, STATIC_SETTING_NAMES


def valid_config(**values):
    """使用独立测试密钥构造配置，不读取启动文件。"""
    return RuntimeConfig(
        onebot_v11_access_token="offline-onebot-token-longer-than-thirty-two-characters",
        chat_session_secret="offline-session-secret-longer-than-thirty-two-characters",
        **values,
    )


def test_plugin_selection_is_dynamic_but_requires_restart():
    """缺少新键的老快照仍启用 QQ，新键只写数据库。"""
    config = valid_config()
    assert config.app_enabled_plugins == ["qqbot"]
    assert "app_enabled_plugins" in RESTART_REQUIRED_FIELDS
    assert "app_enabled_plugins" in DYNAMIC_SETTING_NAMES
    assert "app_enabled_plugins" not in STATIC_SETTING_NAMES
    assert valid_config(app_enabled_plugins=[]).app_enabled_plugins == []


@pytest.mark.parametrize("value", [["unknown"], ["qqbot", "qqbot"], "qqbot", None, [1]])
def test_invalid_plugin_selection_is_rejected(value):
    """任意导入路径、重复 ID 和非法类型不能成为插件加载请求。"""
    with pytest.raises(ValidationError):
        valid_config(app_enabled_plugins=value)


def test_sqlite_saves_selection_and_legacy_missing_key_keeps_qq(tmp_path):
    """真实临时 SQLite 验证保存、冲突与缺少新键的旧快照，不触及生产库。"""
    from contextlib import closing

    from agent_bot.database import connect_database, initialize_database
    from agent_bot.settings import DatabaseParams, SettingsRevisionConflictError, SettingsStore

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "test.sqlite3"))
    initialize_database(params)
    store = SettingsStore(params, static_config=valid_config())
    store.initialize(valid_config())
    saved = store.save(valid_config(app_enabled_plugins=[]), expected_revision=1)
    assert saved.revision == 2
    assert store.load().config.app_enabled_plugins == []
    with pytest.raises(SettingsRevisionConflictError):
        store.save(valid_config(), expected_revision=1)
    with closing(connect_database(params)) as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM chat_setting WHERE config_key=%s", ("app_enabled_plugins",))
        connection.commit()
    assert store.load().config.app_enabled_plugins == ["qqbot"]
