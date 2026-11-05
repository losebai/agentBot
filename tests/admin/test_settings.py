"""Configuration management must preserve secrets, validation and revision semantics."""

import pytest
from pydantic import ValidationError


def service(store, runtime):
    """Import the new service at test time so the missing implementation fails clearly."""
    from agent_bot.admin.settings import SettingsService

    return SettingsService(store, runtime)


def update(revision, **values):
    """Build a typed partial update through the public request model."""
    from agent_bot.admin.models import SettingsUpdate

    return SettingsUpdate(expected_revision=revision, values=values)


def test_read_never_discloses_secrets(admin_store, admin_runtime):
    snapshot = service(admin_store, admin_runtime).read()
    body = snapshot.model_dump_json()
    assert "offline-role-secret" not in body
    assert "offline-onebot-token" not in body
    field = next(item for item in snapshot.fields if item.name == "chat_naraka_default_role_id")
    assert field.value is None and field.secret_is_set
    assert snapshot.database_revision == snapshot.applied_revision == 1


def test_removed_search_binding_is_ignored_and_cleaned_on_save(
    admin_store, admin_runtime, fake_mysql,
):
    """旧搜索绑定不再进入配置或影响请求，正常保存会清除历史数据库行。"""
    fake_mysql.table.append({
        "config_key": "chat_python_search_mcp_code", "config_value": '"search"',
    })
    fake_mysql.commit()
    manager = service(admin_store, admin_runtime)
    assert "chat_python_search_mcp_code" not in {
        item.name for item in manager.read().fields
    }
    result = manager.update(update(1, chat_llm_temperature=0.4))
    assert "chat_python_search_mcp_code" not in {item.name for item in result.fields}
    assert not hasattr(admin_store.load().config, "chat_python_search_mcp_code")
    assert "chat_python_search_mcp_code" not in admin_store.load_values()[0]


def test_structured_values_use_json_kind_without_confusing_generic_element_types(
    admin_store, admin_runtime,
):
    """dict[str, str] 和包含标量输入类型的群列表仍须以对象或数组展示。"""
    fields = {item.name: item for item in service(admin_store, admin_runtime).read().fields}
    for name in ("chat_keyword_at_replies", "onebot_v11_api_roots", "chat_auto_speak_groups"):
        assert isinstance(fields[name].value, (dict, list))
        assert fields[name].kind == "json", name
    for field in fields.values():
        if isinstance(field.value, (dict, list)):
            assert field.kind == "json", field.name
    assert fields["chat_llm_enable_thinking"].kind == "boolean"
    assert fields["chat_llm_temperature"].kind == "number"


def test_partial_update_preserves_omitted_secret(admin_store, admin_runtime):
    result = service(admin_store, admin_runtime).update(
        update(1, chat_llm_temperature=0.4)
    )
    saved = admin_store.load()
    assert (saved.config.chat_naraka_default_role_id
            == admin_runtime.config.chat_naraka_default_role_id)
    assert saved.config.chat_llm_temperature == 0.4
    assert result.database_revision == 2 and result.applied_revision == 1


def test_optional_secret_can_be_explicitly_cleared(admin_store, admin_runtime):
    service(admin_store, admin_runtime).update(update(1, chat_naraka_default_role_id=None))
    assert admin_store.load().config.chat_naraka_default_role_id is None


@pytest.mark.parametrize("maximum", [None, 5, 50, 10000])
def test_tool_call_budget_can_be_saved_without_builtin_ceiling(
    admin_store, admin_runtime, maximum,
):
    """设置持久化正整数或无限制，旧联网预算隐藏且不影响统一配置。"""
    manager = service(admin_store, admin_runtime)
    result = manager.update(update(1, chat_agent_max_steps=maximum))
    fields = {item.name: item for item in result.fields}
    assert fields["chat_agent_max_steps"].value == maximum
    assert "chat_web_search_max_steps" not in fields
    assert admin_store.load().config.chat_agent_max_steps == maximum


def test_robot_system_prompt_can_be_explicitly_cleared(admin_store, admin_runtime):
    """管理页保存空提示词后，读取仍返回空值而不是初始默认文本。"""
    manager = service(admin_store, admin_runtime)
    manager.update(update(1, chat_system_prompt=""))
    assert admin_store.load().config.chat_system_prompt == ""
    field = next(item for item in manager.read().fields if item.name == "chat_system_prompt")
    assert field.value == ""


@pytest.mark.parametrize("maximum", [1, 120_000, 500_000, 2_000_000])
def test_context_budget_is_saved_as_runtime_setting(admin_store, admin_runtime, maximum):
    """上下文正整数预算可超过旧硬编码值，保存后等待现有热刷新而非进程重启。"""
    manager = service(admin_store, admin_runtime)
    result = manager.update(update(1, chat_llm_context_max_chars=maximum))
    field = next(f for f in result.fields if f.name == "chat_llm_context_max_chars")
    assert field.kind == "number" and field.value == maximum
    assert not field.restart_required and not field.nullable
    assert admin_store.load().config.chat_llm_context_max_chars == maximum
    assert result.database_revision == 2 and result.applied_revision == 1


@pytest.mark.parametrize("value", [0, -1, None, 1.5])
def test_context_budget_rejects_invalid_values(admin_store, admin_runtime, value):
    """上下文预算必须是正整数，不能把空值或非整数静默转换为默认值。"""
    with pytest.raises(ValidationError):
        service(admin_store, admin_runtime).update(update(1, chat_llm_context_max_chars=value))
    assert admin_store.load().revision == 1


def test_conflicting_revision_preserves_saved_configuration(admin_store, admin_runtime):
    from agent_bot.settings import SettingsRevisionConflictError

    manager = service(admin_store, admin_runtime)
    manager.update(update(1, chat_llm_temperature=0.4))
    with pytest.raises(SettingsRevisionConflictError):
        manager.update(update(1, chat_llm_temperature=0.7))
    assert admin_store.load().config.chat_llm_temperature == 0.4


def test_static_fields_are_read_only_and_api_rejects_changes(admin_store, admin_runtime):
    """静态配置公开当前值但不可保存，不能通过请求绕过页面只读。"""
    manager = service(admin_store, admin_runtime)
    port = next(field for field in manager.read().fields if field.name == "port")
    assert port.scope == "static" and port.read_only and port.value == 8080
    with pytest.raises(ValidationError):
        manager.update(update(1, port=8765))
    assert admin_store.load().revision == 1


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"made_up_setting": "secret-input"},
        {"chat_ai_base_url": "https://removed.invalid"},
        {"chat_ai_token": "removed-private-token"},
        {"chat_plain_ai_backend": "java"},
        {"chat_agent_backend": "java"},
        {"chat_media_backend": "java"},
        {"chat_python_search_mcp_code": "search"},
        {"chat_naraka_default_role_id": "**********"},
        {"chat_llm_temperature": -1},
        {"onebot_v11_access_token": None},
    ],
)
def test_invalid_patch_does_not_write(admin_store, admin_runtime, values):
    with pytest.raises(ValueError):
        service(admin_store, admin_runtime).update(update(1, **values))
    assert admin_store.load().revision == 1


def test_request_error_hides_input(admin_store, admin_runtime):
    with pytest.raises(ValidationError) as failure:
        update(1, made_up_setting="private-sensitive-input")
    assert "private-sensitive-input" not in str(failure.value)
