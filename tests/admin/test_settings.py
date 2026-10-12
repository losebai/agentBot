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
    assert "offline-ai-secret" not in body
    assert "offline-onebot-token" not in body
    field = next(item for item in snapshot.fields if item.name == "dispatch_chat_ai_token")
    assert field.value is None and field.secret_is_set
    assert snapshot.database_revision == snapshot.applied_revision == 1


def test_partial_update_preserves_omitted_secret(admin_store, admin_runtime):
    result = service(admin_store, admin_runtime).update(
        update(1, dispatch_chat_llm_temperature=0.4)
    )
    saved = admin_store.load()
    assert saved.config.dispatch_chat_ai_token == admin_runtime.config.dispatch_chat_ai_token
    assert saved.config.dispatch_chat_llm_temperature == 0.4
    assert result.database_revision == 2 and result.applied_revision == 1


def test_optional_secret_can_be_explicitly_cleared(admin_store, admin_runtime):
    service(admin_store, admin_runtime).update(update(1, dispatch_chat_ai_token=None))
    assert admin_store.load().config.dispatch_chat_ai_token is None


def test_conflicting_revision_preserves_saved_configuration(admin_store, admin_runtime):
    from agent_bot.settings import SettingsRevisionConflictError

    manager = service(admin_store, admin_runtime)
    manager.update(update(1, dispatch_chat_llm_temperature=0.4))
    with pytest.raises(SettingsRevisionConflictError):
        manager.update(update(1, dispatch_chat_llm_temperature=0.7))
    assert admin_store.load().config.dispatch_chat_llm_temperature == 0.4


def test_restart_fields_compare_saved_and_running_configuration(admin_store, admin_runtime):
    result = service(admin_store, admin_runtime).update(update(1, port=8765))
    assert result.restart_required_fields == ["port"]
    assert admin_runtime.config.port != 8765


@pytest.mark.parametrize(
    "values",
    [
        {},
        {"made_up_setting": "secret-input"},
        {"dispatch_chat_ai_token": "**********"},
        {"dispatch_chat_llm_temperature": -1},
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
