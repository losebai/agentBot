"""验证 Agent 发布资源、版本一致性和回退时的配置保留。"""
import re
from pathlib import Path

import tomllib

from _fake_mysql import FakeMySQL
from agent_bot.config import RuntimeConfig
from agent_bot.settings import DatabaseParams, SettingsStore, runtime_config_to_plain_values


def test_new_settings_cas_preserves_existing_credentials_and_budgets():
    db = FakeMySQL()
    store = SettingsStore(
        DatabaseParams("localhost", 3306, "offline", "offline", "offline"), connect=db.connect
    )
    config = RuntimeConfig(
        onebot_v11_access_token="offline-token-with-at-least-thirty-two-characters",
        chat_session_secret="offline-secret-with-at-least-thirty-two-characters",
        chat_agent_max_steps=2,
        chat_mcp_services=("docs",),
    )
    first = store.initialize(config)
    values = runtime_config_to_plain_values(first.config)
    values.update(
        chat_python_agent_code="helper",
        chat_python_search_mcp_code="search",
    )
    updated = store.save(values, expected_revision=first.revision)
    assert updated.revision == 2 and updated.config.chat_python_agent_code == "helper"
    values = runtime_config_to_plain_values(updated.config)
    for name in (
        "chat_python_agent_code",
        "chat_python_search_mcp_code",
    ):
        values.pop(name)
    restored = store.save(values, expected_revision=updated.revision)
    assert restored.config.chat_python_agent_code is None
    assert restored.config.chat_agent_max_steps == 2
    assert restored.config.chat_mcp_services == ["docs"]
    assert restored.config.onebot_v11_access_token == config.onebot_v11_access_token


def test_agent_sql_is_a_packaged_nondestructive_resource():
    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    resources = project["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert (
        resources["sql/legacy/agent_bot_agent_init.sql"]
        == "agent_bot/resources/agent_bot_agent_init.sql"
    )
    sql = (root / "sql/legacy/agent_bot_agent_init.sql").read_text(encoding="utf-8")
    assert not re.search(r"\bDROP\b", sql, re.I)
    assert sql.count("CREATE TABLE IF NOT EXISTS") == 7


def test_package_versions_match():
    """源码和项目声明必须使用当前同一发布版本。"""
    import agent_bot

    root = Path(__file__).resolve().parents[2]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    assert agent_bot.__version__ == project["project"]["version"] == "0.5.1"
