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
