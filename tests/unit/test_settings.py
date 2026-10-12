"""配置存储、版本冲突和完整运行字段的离线回归。"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pytest
from pymysql.err import DatabaseError, InterfaceError, OperationalError

# Shared fake import lives one level up; made available via conftest collection.
from _fake_mysql import FakeMySQL
from agent_bot.config import RuntimeConfig
from agent_bot.settings import (
    DatabaseParams,
    SettingsAlreadyInitializedError,
    SettingsError,
    SettingsNotInitializedError,
    SettingsRevisionConflictError,
    SettingsSchemaError,
    SettingsStore,
    SettingsValidationError,
    VersionedSettings,
    runtime_config_to_plain_values,
)

EXPECTED_RUNTIME_FIELDS = {
    "dispatch_chat_ai_base_url",
    "dispatch_chat_plain_ai_backend",
    "dispatch_chat_agent_backend",
    "dispatch_chat_media_backend",
    "dispatch_chat_python_agent_code",
    "dispatch_chat_python_search_mcp_code",
    "dispatch_chat_model",
    "dispatch_chat_vision_model",
    "dispatch_chat_image_model",
    "dispatch_chat_system_prompt",
    "dispatch_chat_ai_token",
    "dispatch_chat_llm_max_tokens",
    "dispatch_chat_llm_temperature",
    "dispatch_chat_llm_top_p",
    "dispatch_chat_llm_top_k",
    "dispatch_chat_llm_min_p",
    "dispatch_chat_llm_presence_penalty",
    "dispatch_chat_llm_frequency_penalty",
    "dispatch_chat_llm_repetition_penalty",
    "dispatch_chat_llm_stop",
    "dispatch_chat_llm_enable_thinking",
    "dispatch_chat_llm_forward_user_name",
    "dispatch_chat_agent_enabled",
    "dispatch_chat_agent_max_steps",
    "dispatch_chat_mcp_services",
    "dispatch_chat_web_search_enabled",
    "dispatch_chat_web_search_max_steps",
    "dispatch_chat_image_recognition_enabled",
    "dispatch_chat_max_images_per_message",
    "dispatch_chat_image_generation_enabled",
    "dispatch_chat_image_timeout_seconds",
    "dispatch_chat_image_response_format",
    "dispatch_chat_image_max_bytes",
    "dispatch_chat_local_workflow_enabled",
    "dispatch_chat_local_workflow_base_url",
    "dispatch_chat_local_workflow_directory",
    "dispatch_chat_local_workflow_default_key",
    "dispatch_chat_local_workflow_timeout_seconds",
    "dispatch_chat_keyword_at_replies",
    "dispatch_chat_auto_mention_enabled",
    "dispatch_chat_mention_tool_enabled",
    "dispatch_chat_tts_enabled",
    "dispatch_chat_tts_url",
    "dispatch_chat_tts_token",
    "dispatch_chat_tts_model",
    "dispatch_chat_tts_voice",
    "dispatch_chat_tts_response_format",
    "dispatch_chat_tts_speed",
    "dispatch_chat_tts_timeout_seconds",
    "dispatch_chat_tts_max_input_chars",
    "dispatch_chat_tts_max_audio_bytes",
    "dispatch_chat_tts_reference_audio_path",
    "dispatch_chat_tts_reference_text",
    "dispatch_chat_tts_reference_language",
    "dispatch_chat_tts_text_language",
    "dispatch_chat_tts_output_directory",
    "dispatch_chat_mcp_enabled",
    "dispatch_chat_laya_enabled",
    "dispatch_chat_job_enabled",
    "dispatch_chat_soloncode_enabled",
    "dispatch_chat_job_poll_seconds",
    "dispatch_chat_mcp_mount_path",
    "dispatch_chat_mcp_allowed_hosts",
    "dispatch_chat_mcp_session_ttl_seconds",
    "dispatch_chat_mcp_max_sessions",
    "onebot_v11_access_token",
    "dispatch_chat_session_secret",
    "dispatch_chat_naraka_cookie",
    "dispatch_chat_naraka_heybox_id",
    "dispatch_chat_naraka_default_role_id",
    "dispatch_chat_naraka_server",
    "dispatch_chat_naraka_season",
    "dispatch_chat_naraka_battle_tid",
    "dispatch_chat_auto_speak_enabled",
    "dispatch_chat_auto_speak_bot_id",
    "dispatch_chat_auto_speak_groups",
    "dispatch_chat_auto_speak_users",
    "dispatch_chat_auto_speak_trigger_mode",
    "dispatch_chat_auto_speak_prompt",
    "dispatch_chat_auto_speak_laya_prompt",
    "dispatch_chat_auto_speak_initial_delay_seconds",
    "dispatch_chat_auto_speak_interval_seconds",
    "dispatch_chat_auto_speak_max_reply_chars",
    "dispatch_chat_timeout_seconds",
    "dispatch_chat_max_input_chars",
    "dispatch_chat_history_turns",
    "dispatch_chat_history_max_chars",
    "dispatch_chat_history_ttl_seconds",
    "dispatch_chat_group_recent_context_enabled",
    "dispatch_chat_group_recent_context_messages",
    "dispatch_chat_group_recent_context_max_chars",
    "dispatch_chat_group_recent_context_ttl_seconds",
    "dispatch_chat_memory_enabled",
    "dispatch_chat_tagged_memory_enabled",
    "dispatch_chat_memory_max_chars",
    "dispatch_chat_memory_compression_max_tokens",
    "dispatch_chat_memory_compression_queue_capacity",
    "dispatch_chat_db_pool_size",
    "dispatch_chat_db_timeout_seconds",
    "dispatch_chat_model_cache_ttl_seconds",
    "dispatch_chat_max_sessions",
    "dispatch_chat_max_concurrent_requests",
    "dispatch_chat_cooldown_seconds",
    "dispatch_chat_chunk_chars",
    "dispatch_chat_max_reply_chars",
    "dispatch_chat_queue_max_pending",
    "dispatch_chat_queue_workers",
    "dispatch_chat_queue_media_workers",
    "dispatch_chat_queue_max_attempts",
    "dispatch_chat_queue_retry_initial_seconds",
    "dispatch_chat_queue_retry_max_seconds",
    "dispatch_chat_queue_job_ttl_seconds",
    "dispatch_chat_queue_result_ttl_seconds",
    "dispatch_chat_naraka_timeout_seconds",
    "dispatch_chat_naraka_cooldown_seconds",
    "dispatch_chat_naraka_max_concurrent_requests",
    "dispatch_chat_allowed_groups",
    "dispatch_chat_allowed_users",
    "dispatch_chat_blocked_groups",
    "dispatch_chat_blocked_users",
    "dispatch_chat_allow_all",
    "dispatch_chat_group_shared_session",
    "dispatch_chat_priority",
    "dispatch_chat_reset_priority",
    "dispatch_chat_user_message_limit",
    "dispatch_chat_user_message_window_seconds",
    "dispatch_chat_config_refresh_interval_seconds",
    "driver",
    "host",
    "port",
    "log_level",
    "command_start",
    "api_timeout",
    "superusers",
    "nickname",
    "command_sep",
    "session_expire_timeout",
    "onebot_v11_secret",
    "onebot_v11_ws_urls",
    "onebot_v11_api_roots",
}


@pytest.fixture
def database_params() -> DatabaseParams:
    return DatabaseParams(
        host="127.0.0.1",
        port=3306,
        user="dispatch_chat",
        password="secret",
        database="dispatch_chat",
    )


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    """构造覆盖所有运行字段的配置，媒体开关也必须参与持久化。"""
    values: dict[str, object] = {
        "dispatch_chat_ai_base_url": "https://ai.internal.example.invalid/dispatch-ai",
        "dispatch_chat_plain_ai_backend": "java",
        "dispatch_chat_agent_backend": "python",
        "dispatch_chat_media_backend": "python",
        "dispatch_chat_python_agent_code": "helper",
        "dispatch_chat_python_search_mcp_code": "search",
        "dispatch_chat_model": "private-model-db-snapshot-test",
        "dispatch_chat_vision_model": "private-vision-model-db-snapshot-test",
        "dispatch_chat_image_model": "private-image-model-db-snapshot-test",
        "dispatch_chat_system_prompt": "ordinary-setting-plaintext-must-not-appear",
        "dispatch_chat_ai_token": "ai-token-plaintext-marker",
        "dispatch_chat_llm_max_tokens": 4096,
        "dispatch_chat_llm_temperature": 0.6,
        "dispatch_chat_llm_top_p": 0.8,
        "dispatch_chat_llm_top_k": 40,
        "dispatch_chat_llm_min_p": 0.05,
        "dispatch_chat_llm_presence_penalty": 0.1,
        "dispatch_chat_llm_frequency_penalty": 0.2,
        "dispatch_chat_llm_repetition_penalty": 1.1,
        "dispatch_chat_llm_stop": ["private-stop-marker"],
        "dispatch_chat_llm_enable_thinking": True,
        "dispatch_chat_llm_forward_user_name": True,
        "dispatch_chat_agent_enabled": False,
        "dispatch_chat_agent_max_steps": 4,
        "dispatch_chat_mcp_services": ["windows-mcp"],
        "dispatch_chat_web_search_enabled": True,
        "dispatch_chat_web_search_max_steps": 3,
        "dispatch_chat_image_recognition_enabled": True,
        "dispatch_chat_max_images_per_message": 4,
        "dispatch_chat_image_generation_enabled": True,
        "dispatch_chat_image_timeout_seconds": 72,
        "dispatch_chat_image_response_format": "webp",
        "dispatch_chat_image_max_bytes": 8_000_000,
        "dispatch_chat_local_workflow_enabled": True,
        "dispatch_chat_local_workflow_base_url": "http://comfy.internal.example.invalid:8188",
        "dispatch_chat_local_workflow_directory": "C:/dispatch/workflows",
        "dispatch_chat_local_workflow_default_key": "sdxl-lightning-4step",
        "dispatch_chat_local_workflow_timeout_seconds": 180,
        "dispatch_chat_keyword_at_replies": {
            "private-keyword-marker": "private-keyword-reply-marker"
        },
        "dispatch_chat_auto_mention_enabled": False,
        "dispatch_chat_mention_tool_enabled": True,
        "dispatch_chat_tts_enabled": True,
        "dispatch_chat_tts_url": "https://tts.internal.example.invalid/v1/audio/speech",
        "dispatch_chat_tts_token": "tts-token-plaintext-marker",
        "dispatch_chat_tts_model": "private-speech-model",
        "dispatch_chat_tts_voice": "private-speech-voice",
        "dispatch_chat_tts_response_format": "opus",
        "dispatch_chat_tts_speed": 1.2,
        "dispatch_chat_tts_timeout_seconds": 21.5,
        "dispatch_chat_tts_max_input_chars": 777,
        "dispatch_chat_tts_max_audio_bytes": 4_000_000,
        "dispatch_chat_tts_reference_audio_path": "G:/models/hutao/reference.wav",
        "dispatch_chat_tts_reference_text": "胡桃参考文本",
        "dispatch_chat_tts_reference_language": "zh",
        "dispatch_chat_tts_text_language": "zh",
        "dispatch_chat_tts_output_directory": "G:/dispatch/tts-output",
        "dispatch_chat_mcp_enabled": True,
        "dispatch_chat_laya_enabled": True,
        "dispatch_chat_job_enabled": False,
        "dispatch_chat_soloncode_enabled": False,
        "dispatch_chat_job_poll_seconds": 7.5,
        "dispatch_chat_mcp_mount_path": "/private-mcp",
        "dispatch_chat_mcp_allowed_hosts": ["chat.internal.example.invalid:*"],
        "dispatch_chat_mcp_session_ttl_seconds": 87.5,
        "dispatch_chat_mcp_max_sessions": 321,
        "onebot_v11_access_token": "onebot-token-plaintext-marker-0123456789-abcdefgh",
        "dispatch_chat_session_secret": "session-secret-plaintext-marker-0123456789-abcdef",
        "dispatch_chat_naraka_cookie": (
            "pkey=cookie-private-pkey-value; x_heybox_id=731946285017; "
            "x_pkey=cookie-private-x-pkey-value; x_xhh_tokenid=cookie-private-token-id"
        ),
        "dispatch_chat_naraka_heybox_id": "731946285017",
        "dispatch_chat_naraka_default_role_id": "Role_Private_731946285017",
        "dispatch_chat_naraka_server": "server_private",
        "dispatch_chat_naraka_season": "season_private",
        "dispatch_chat_naraka_battle_tid": "battle_private",
        "dispatch_chat_auto_speak_enabled": True,
        "dispatch_chat_auto_speak_bot_id": "940000000000001",
        "dispatch_chat_auto_speak_groups": {"940000000000002"},
        "dispatch_chat_auto_speak_users": {"940000000000003"},
        "dispatch_chat_auto_speak_trigger_mode": "message",
        "dispatch_chat_auto_speak_prompt": "scheduled-ordinary-setting-must-be-encrypted",
        "dispatch_chat_auto_speak_laya_prompt": (
            "scheduled-laya-setting-must-be-encrypted"
        ),
        "dispatch_chat_auto_speak_initial_delay_seconds": 17,
        "dispatch_chat_auto_speak_interval_seconds": 73,
        "dispatch_chat_auto_speak_max_reply_chars": 499,
        "dispatch_chat_timeout_seconds": 91.25,
        "dispatch_chat_max_input_chars": 2345,
        "dispatch_chat_history_turns": 8,
        "dispatch_chat_history_max_chars": 13_579,
        "dispatch_chat_history_ttl_seconds": 1901.5,
        "dispatch_chat_group_recent_context_enabled": True,
        "dispatch_chat_group_recent_context_messages": 17,
        "dispatch_chat_group_recent_context_max_chars": 6789,
        "dispatch_chat_group_recent_context_ttl_seconds": 421.5,
        "dispatch_chat_memory_enabled": True,
        "dispatch_chat_tagged_memory_enabled": False,
        "dispatch_chat_memory_max_chars": 4567,
        "dispatch_chat_memory_compression_max_tokens": 768,
        "dispatch_chat_memory_compression_queue_capacity": 17,
        "dispatch_chat_db_pool_size": 3,
        "dispatch_chat_db_timeout_seconds": 13,
        "dispatch_chat_model_cache_ttl_seconds": 2.5,
        "dispatch_chat_max_sessions": 987,
        "dispatch_chat_max_concurrent_requests": 7,
        "dispatch_chat_cooldown_seconds": 3.25,
        "dispatch_chat_chunk_chars": 777,
        "dispatch_chat_max_reply_chars": 4321,
        "dispatch_chat_queue_max_pending": 876,
        "dispatch_chat_queue_workers": 6,
        "dispatch_chat_queue_media_workers": 2,
        "dispatch_chat_queue_max_attempts": 4,
        "dispatch_chat_queue_retry_initial_seconds": 1.5,
        "dispatch_chat_queue_retry_max_seconds": 18.5,
        "dispatch_chat_queue_job_ttl_seconds": 1200,
        "dispatch_chat_queue_result_ttl_seconds": 2400,
        "dispatch_chat_naraka_timeout_seconds": 16.5,
        "dispatch_chat_naraka_cooldown_seconds": 11.5,
        "dispatch_chat_naraka_max_concurrent_requests": 3,
        "dispatch_chat_allowed_groups": {"940000000000004"},
        "dispatch_chat_allowed_users": {"940000000000005"},
        "dispatch_chat_blocked_groups": {"940000000000007"},
        "dispatch_chat_blocked_users": {"940000000000008"},
        "dispatch_chat_allow_all": False,
        "dispatch_chat_group_shared_session": True,
        "dispatch_chat_priority": 71,
        "dispatch_chat_reset_priority": 13,
        "dispatch_chat_user_message_limit": 23,
        "dispatch_chat_user_message_window_seconds": 1800,
        "dispatch_chat_config_refresh_interval_seconds": 7.5,
        "driver": "~fastapi",
        "host": "203.0.113.77",
        "port": 18_081,
        "log_level": "DEBUG",
        "command_start": {"/", "!"},
        "api_timeout": 31.5,
        "superusers": {"940000000000006"},
        "nickname": {"private-bot-nickname-marker"},
        "command_sep": {".", ":"},
        "session_expire_timeout": timedelta(seconds=321),
        "onebot_v11_secret": "onebot-http-signing-secret-marker",
        "onebot_v11_ws_urls": {"wss://onebot-private.example.invalid/ws-private-marker"},
        "onebot_v11_api_roots": {
            "primary": "https://onebot-private.example.invalid/api-private-marker"
        },
    }
    assert len(values) == 140
    assert set(RuntimeConfig.model_fields) == EXPECTED_RUNTIME_FIELDS == set(values)
    return RuntimeConfig(**values)


def _make_store(database_params: DatabaseParams) -> SettingsStore:
    # fake_mysql fixture 已经把 pymysql.connect 替换为共享实例。
    return SettingsStore(database_params)


def test_round_trips_all_runtime_settings(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    """全部配置字段经数据库往返后保持值及敏感字段语义。"""
    store = _make_store(database_params)

    initialized = store.initialize(runtime_config)
    loaded = store.load()

    assert initialized == VersionedSettings(config=runtime_config, revision=1)
    assert loaded == initialized
    assert len(type(loaded.config).model_fields) == 140
    for field_name in EXPECTED_RUNTIME_FIELDS:
        assert getattr(loaded.config, field_name) == getattr(runtime_config, field_name)


def test_existing_snapshot_without_chat_agent_steps_keeps_default_behavior(
    database_params: DatabaseParams, fake_mysql: FakeMySQL, runtime_config: RuntimeConfig,
) -> None:
    """老配置表缺少新增轮次字段时可以加载，不需要数据库迁移。"""
    store = _make_store(database_params)
    store.initialize(runtime_config)
    fake_mysql.table[:] = [
        row for row in fake_mysql.table if row["config_key"] != "dispatch_chat_agent_max_steps"
    ]

    loaded = store.load()

    assert loaded.config.dispatch_chat_agent_max_steps is None
    assert loaded.config.dispatch_chat_web_search_max_steps == 3


def test_message_trigger_snapshot_accepts_unused_sub_minute_interval(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    store = _make_store(database_params)
    store.initialize(runtime_config)
    replacements = {
        "dispatch_chat_auto_speak_trigger_mode": '"message"',
        "dispatch_chat_auto_speak_interval_seconds": "1",
    }
    for row in fake_mysql.table:
        if row["config_key"] in replacements:
            row["config_value"] = replacements[row["config_key"]]

    loaded = store.load()

    assert loaded.config.dispatch_chat_auto_speak_trigger_mode == "message"
    assert loaded.config.dispatch_chat_auto_speak_interval_seconds == 1


@pytest.mark.parametrize(
    ("field_name", "raw_text"),
    [
        (
            "dispatch_chat_system_prompt",
            (
                "# 角色说明\n\n"
                "保留 `代码`、\"引号\" 和路径 `C:\\models`。\n\n"
                "```json\n{\"enabled\": true}\n```"
            ),
        ),
        ("dispatch_chat_local_workflow_directory", "C:\\dispatch\\workflows"),
    ],
)
def test_load_accepts_raw_text_for_string_setting(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
    field_name: str,
    raw_text: str,
) -> None:
    """必填或可空字符串配置均可读取原始文本，包括多行 Markdown。"""

    store = _make_store(database_params)
    store.initialize(runtime_config)
    for row in fake_mysql.table:
        if row["config_key"] == field_name:
            row["config_value"] = raw_text
            break

    loaded = store.load()

    assert str(getattr(loaded.config, field_name)) == raw_text


def test_load_accepts_raw_text_for_secret_string_setting(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    """SecretStr 配置也可兼容数据库中的未编码原始文本。"""

    raw_token = "raw-secret-string-placeholder"
    store = _make_store(database_params)
    store.initialize(runtime_config)
    for row in fake_mysql.table:
        if row["config_key"] == "dispatch_chat_ai_token":
            row["config_value"] = raw_token
            break

    loaded = store.load()

    assert loaded.config.dispatch_chat_ai_token is not None
    assert loaded.config.dispatch_chat_ai_token.get_secret_value() == raw_token


def test_load_rejects_raw_text_fallback_for_non_string_setting(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    """非字符串配置不能借助 Pydantic 类型转换绕过 JSON 格式。"""

    store = _make_store(database_params)
    store.initialize(runtime_config)
    for row in fake_mysql.table:
        if row["config_key"] == "dispatch_chat_agent_enabled":
            row["config_value"] = "yes"
            break

    with pytest.raises(SettingsValidationError):
        store.load()


def test_invalid_snapshot_reports_field_and_rule_without_exposing_value(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    """热刷新失败原因应定位字段和规则，但不得输出数据库原值。"""

    store = _make_store(database_params)
    store.initialize(runtime_config)
    invalid_value = -987654321
    for row in fake_mysql.table:
        if row["config_key"] == "dispatch_chat_llm_max_tokens":
            row["config_value"] = str(invalid_value)
            break

    with pytest.raises(SettingsValidationError) as exc_info:
        store.load()

    reason = str(exc_info.value)
    assert reason == (
        "settings snapshot failed validation: "
        "dispatch_chat_llm_max_tokens[greater_than_equal]"
    )
    assert str(invalid_value) not in reason


def test_uninitialized_store_rejects_load_and_save(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    store = _make_store(database_params)

    with pytest.raises(SettingsNotInitializedError):
        store.load()
    with pytest.raises(SettingsNotInitializedError):
        store.save(runtime_config, expected_revision=1)


def test_database_params_repr_does_not_expose_password(
    database_params: DatabaseParams,
) -> None:
    assert database_params.password not in repr(database_params)


def test_initialize_twice_is_rejected_without_overwriting_original(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    store = _make_store(database_params)
    original = store.initialize(runtime_config)
    replacement = runtime_config.model_copy(update={"dispatch_chat_model": "replacement"})

    with pytest.raises(SettingsAlreadyInitializedError):
        store.initialize(replacement)

    assert store.load() == original


def test_save_uses_optimistic_revision_locking(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    store = _make_store(database_params)
    original = store.initialize(runtime_config)
    updated_config = runtime_config.model_copy(update={"dispatch_chat_model": "revision-two"})

    updated = store.save(updated_config, expected_revision=original.revision)

    assert updated == VersionedSettings(config=updated_config, revision=2)
    stale_candidate = runtime_config.model_copy(update={"dispatch_chat_model": "stale-write"})
    with pytest.raises(SettingsRevisionConflictError):
        store.save(stale_candidate, expected_revision=original.revision)
    assert store.load() == updated


def test_load_revision_reads_only_current_version(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    store = _make_store(database_params)
    store.initialize(runtime_config)

    assert store.load_revision() == 1

    updated = runtime_config.model_copy(update={"dispatch_chat_model": "revision-two"})
    store.save(updated, expected_revision=1)
    assert store.load_revision() == 2


def test_invalid_candidate_does_not_overwrite_previous_revision(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    store = _make_store(database_params)
    original = store.initialize(runtime_config)
    invalid_candidate = runtime_config.model_copy(
        update={
            "dispatch_chat_chunk_chars": 5000,
            "dispatch_chat_max_reply_chars": 100,
        }
    )

    with pytest.raises(SettingsValidationError):
        store.save(invalid_candidate, expected_revision=original.revision)

    assert store.load() == original


def test_load_drops_legacy_keys_removed_from_model(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    """数据库快照中残留已删除字段时，load() 必须丢弃而非拒绝，与 extra="ignore" 一致。"""

    store = _make_store(database_params)
    original = store.initialize(runtime_config)

    # 模拟历史版本写入、当前模型已移除的字段。
    fake_mysql.table.append(
        {
            "config_key": "dispatch_chat_legacy_removed_field",
            "config_value": '"stale-value"',
            "description": "",
        }
    )

    loaded = store.load()
    assert loaded == original
    plain = runtime_config_to_plain_values(loaded.config)
    assert "dispatch_chat_legacy_removed_field" not in plain


def test_load_rejects_missing_schema_tables(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
) -> None:
    fake_mysql.table_columns.clear()
    store = _make_store(database_params)

    with pytest.raises(SettingsSchemaError, match="schema is invalid"):
        store.load()


def test_load_rejects_schema_with_missing_columns(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
) -> None:
    fake_mysql.table_columns["dispatch_chat_setting"] = ["id", "config_key"]
    store = _make_store(database_params)

    with pytest.raises(SettingsSchemaError, match="schema is invalid"):
        store.load()


def test_runtime_config_storage_contract_covers_all_plugin_and_nonebot_fields() -> None:
    """完整字段集合与插件运行配置一致，防止新增媒体开关漏存。"""
    from nonebot.config import Config as NoneBotConfig

    expected_nonebot_fields = set(NoneBotConfig.model_fields)
    expected_onebot_fields = {
        "onebot_v11_secret",
        "onebot_v11_ws_urls",
        "onebot_v11_api_roots",
    }
    plugin_fields = set(RuntimeConfig.__mro__[1].model_fields)

    assert set(RuntimeConfig.model_fields) == (
        plugin_fields | expected_nonebot_fields | expected_onebot_fields
    )
    assert len(RuntimeConfig.model_fields) == 140


def test_runtime_config_to_plain_values_unpacks_secretstr_and_collections() -> None:
    config = RuntimeConfig(
        onebot_v11_access_token="x" * 40,
        dispatch_chat_session_secret="y" * 40,
        superusers={"123"},
        session_expire_timeout=timedelta(seconds=90),
    )
    values = runtime_config_to_plain_values(config)

    assert values["onebot_v11_access_token"] == "x" * 40
    assert values["superusers"] == ["123"]
    assert values["session_expire_timeout"] == 90.0


@pytest.mark.parametrize(
    ("exc", "operation"),
    [
        (InterfaceError("raw driver detail"), "load"),
        (DatabaseError("raw driver detail"), "initialize"),
        (OperationalError("raw driver detail"), "save"),
    ],
)
def test_public_store_operations_wrap_database_errors_consistently(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
    exc: Exception,
    operation: str,
) -> None:
    store = _make_store(database_params)
    store._open = Mock(side_effect=exc)  # type: ignore[method-assign]

    with pytest.raises(SettingsError) as exc_info:
        if operation == "initialize":
            store.initialize(runtime_config)
        elif operation == "load":
            store.load()
        else:
            store.save(runtime_config, expected_revision=1)

    assert type(exc_info.value) is SettingsError
    assert str(exc_info.value) == "settings database operation failed"


def test_database_params_is_immutable() -> None:
    params = DatabaseParams(
        host="db.example",
        port=3307,
        user="u",
        password="p",
        database="d",
    )
    with pytest.raises(AttributeError):
        params.host = "other"  # type: ignore[misc]
