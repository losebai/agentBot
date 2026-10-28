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
    "app_enabled_plugins",
    "agent_bot_napcat_mode",
    "agent_bot_napcat_webui_port",
    "chat_python_agent_code",
    "chat_model",
    "chat_vision_model",
    "chat_image_model",
    "chat_system_prompt",
    "chat_llm_max_tokens",
    "chat_llm_temperature",
    "chat_llm_top_p",
    "chat_llm_top_k",
    "chat_llm_min_p",
    "chat_llm_presence_penalty",
    "chat_llm_frequency_penalty",
    "chat_llm_repetition_penalty",
    "chat_llm_stop",
    "chat_llm_enable_thinking",
    "chat_llm_forward_user_name",
    "chat_agent_max_steps",
    "chat_mcp_services",
    "chat_web_search_enabled",
    "chat_web_search_max_steps",
    "chat_image_recognition_enabled",
    "chat_max_images_per_message",
    "chat_image_generation_enabled",
    "chat_image_timeout_seconds",
    "chat_image_response_format",
    "chat_image_max_bytes",
    "chat_local_workflow_enabled",
    "chat_local_workflow_base_url",
    "chat_local_workflow_directory",
    "chat_local_workflow_default_key",
    "chat_local_workflow_timeout_seconds",
    "chat_keyword_at_replies",
    "chat_auto_mention_enabled",
    "chat_mention_tool_enabled",
    "chat_tts_enabled",
    "chat_tts_url",
    "chat_tts_token",
    "chat_tts_model",
    "chat_tts_voice",
    "chat_tts_response_format",
    "chat_tts_speed",
    "chat_tts_timeout_seconds",
    "chat_tts_max_input_chars",
    "chat_tts_max_audio_bytes",
    "chat_tts_reference_audio_path",
    "chat_tts_reference_text",
    "chat_tts_reference_language",
    "chat_tts_text_language",
    "chat_tts_output_directory",
    "chat_mcp_enabled",
    "chat_job_enabled",
    "chat_job_poll_seconds",
    "chat_mcp_mount_path",
    "chat_mcp_allowed_hosts",
    "chat_mcp_session_ttl_seconds",
    "chat_mcp_max_sessions",
    "onebot_v11_access_token",
    "chat_session_secret",
    "chat_naraka_cookie",
    "chat_naraka_heybox_id",
    "chat_naraka_default_role_id",
    "chat_naraka_server",
    "chat_naraka_season",
    "chat_naraka_battle_tid",
    "chat_auto_speak_mcp_code",
    "chat_auto_speak_decision_backend",
    "chat_auto_speak_decision_model",
    "chat_auto_speak_llm_prompt",
    "chat_auto_speak_enabled",
    "chat_auto_speak_bot_id",
    "chat_auto_speak_groups",
    "chat_auto_speak_trigger_mode",
    "chat_auto_speak_prompt",
    "chat_auto_speak_laya_prompt",
    "chat_auto_speak_initial_delay_seconds",
    "chat_auto_speak_interval_seconds",
    "chat_auto_speak_message_interval_seconds",
    "chat_auto_speak_max_reply_chars",
    "chat_timeout_seconds",
    "chat_max_input_chars",
    "chat_history_turns",
    "chat_history_max_chars",
    "chat_group_recent_context_enabled",
    "chat_group_recent_context_messages",
    "chat_group_recent_context_max_chars",
    "chat_group_recent_context_ttl_seconds",
    "chat_memory_enabled",
    "chat_tagged_memory_enabled",
    "chat_memory_embedding_model",
    "chat_memory_max_chars",
    "chat_memory_compression_max_tokens",
    "chat_memory_compression_queue_capacity",
    "chat_db_pool_size",
    "chat_db_timeout_seconds",
    "chat_max_sessions",
    "chat_max_concurrent_requests",
    "chat_cooldown_seconds",
    "chat_chunk_chars",
    "chat_max_reply_chars",
    "chat_multi_reply_enabled",
    "chat_multi_reply_max_messages",
    "chat_multi_reply_interval_seconds",
    "chat_multi_reply_prompt",
    "chat_queue_max_pending",
    "chat_queue_workers",
    "chat_queue_media_workers",
    "chat_queue_max_attempts",
    "chat_queue_retry_initial_seconds",
    "chat_queue_retry_max_seconds",
    "chat_queue_job_ttl_seconds",
    "chat_queue_result_ttl_seconds",
    "chat_naraka_timeout_seconds",
    "chat_naraka_cooldown_seconds",
    "chat_naraka_max_concurrent_requests",
    "chat_allowed_groups",
    "chat_allowed_users",
    "chat_blocked_groups",
    "chat_blocked_users",
    "chat_allow_all",
    "chat_group_shared_session",
    "chat_priority",
    "chat_reset_priority",
    "chat_user_message_limit",
    "chat_user_message_window_seconds",
    "chat_config_refresh_interval_seconds",
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
        user="chat",
        password="secret",
        database="chat",
    )


@pytest.fixture
def runtime_config() -> RuntimeConfig:
    """构造覆盖所有运行字段的配置，媒体开关也必须参与持久化。"""
    values: dict[str, object] = {
        "app_enabled_plugins": ["qqbot"],
        "agent_bot_napcat_mode": "external",
        "agent_bot_napcat_webui_port": 6099,
        "chat_python_agent_code": "helper",
        "chat_model": "private-model-db-snapshot-test",
        "chat_vision_model": "private-vision-model-db-snapshot-test",
        "chat_image_model": "private-image-model-db-snapshot-test",
        "chat_system_prompt": "ordinary-setting-plaintext-must-not-appear",
        "chat_llm_max_tokens": 4096,
        "chat_llm_temperature": 0.6,
        "chat_llm_top_p": 0.8,
        "chat_llm_top_k": 40,
        "chat_llm_min_p": 0.05,
        "chat_llm_presence_penalty": 0.1,
        "chat_llm_frequency_penalty": 0.2,
        "chat_llm_repetition_penalty": 1.1,
        "chat_llm_stop": ["private-stop-marker"],
        "chat_llm_enable_thinking": True,
        "chat_llm_forward_user_name": True,
        "chat_agent_max_steps": 4,
        "chat_mcp_services": ["windows-mcp"],
        "chat_web_search_enabled": True,
        "chat_web_search_max_steps": 3,
        "chat_image_recognition_enabled": True,
        "chat_max_images_per_message": 4,
        "chat_image_generation_enabled": True,
        "chat_image_timeout_seconds": 72,
        "chat_image_response_format": "webp",
        "chat_image_max_bytes": 8_000_000,
        "chat_local_workflow_enabled": True,
        "chat_local_workflow_base_url": "http://comfy.internal.example.invalid:8188",
        "chat_local_workflow_directory": "C:/dispatch/workflows",
        "chat_local_workflow_default_key": "sdxl-lightning-4step",
        "chat_local_workflow_timeout_seconds": 180,
        "chat_keyword_at_replies": {
            "private-keyword-marker": "private-keyword-reply-marker"
        },
        "chat_auto_mention_enabled": False,
        "chat_mention_tool_enabled": True,
        "chat_tts_enabled": True,
        "chat_tts_url": "https://tts.internal.example.invalid/v1/audio/speech",
        "chat_tts_token": "tts-token-plaintext-marker",
        "chat_tts_model": "private-speech-model",
        "chat_tts_voice": "private-speech-voice",
        "chat_tts_response_format": "opus",
        "chat_tts_speed": 1.2,
        "chat_tts_timeout_seconds": 21.5,
        "chat_tts_max_input_chars": 777,
        "chat_tts_max_audio_bytes": 4_000_000,
        "chat_tts_reference_audio_path": "G:/models/hutao/reference.wav",
        "chat_tts_reference_text": "胡桃参考文本",
        "chat_tts_reference_language": "zh",
        "chat_tts_text_language": "zh",
        "chat_tts_output_directory": "G:/dispatch/tts-output",
        "chat_mcp_enabled": True,
        "chat_job_enabled": False,
        "chat_job_poll_seconds": 7.5,
        "chat_mcp_mount_path": "/private-mcp",
        "chat_mcp_allowed_hosts": ["chat.internal.example.invalid:*"],
        "chat_mcp_session_ttl_seconds": 87.5,
        "chat_mcp_max_sessions": 321,
        "onebot_v11_access_token": "onebot-token-plaintext-marker-0123456789-abcdefgh",
        "chat_session_secret": "session-secret-plaintext-marker-0123456789-abcdef",
        "chat_naraka_cookie": (
            "pkey=cookie-private-pkey-value; x_heybox_id=731946285017; "
            "x_pkey=cookie-private-x-pkey-value; x_xhh_tokenid=cookie-private-token-id"
        ),
        "chat_naraka_heybox_id": "731946285017",
        "chat_naraka_default_role_id": "Role_Private_731946285017",
        "chat_naraka_server": "server_private",
        "chat_naraka_season": "season_private",
        "chat_naraka_battle_tid": "battle_private",
        "chat_auto_speak_mcp_code": "decision",
        "chat_auto_speak_decision_backend": "llm",
        "chat_auto_speak_decision_model": "model:17",
        "chat_auto_speak_llm_prompt": "independent-llm-prompt-private-marker",
        "chat_auto_speak_enabled": True,
        "chat_auto_speak_bot_id": "940000000000001",
        "chat_auto_speak_groups": {"940000000000002"},
        "chat_auto_speak_trigger_mode": "message",
        "chat_auto_speak_prompt": "scheduled-ordinary-setting-must-be-encrypted",
        "chat_auto_speak_laya_prompt": (
            "scheduled-laya-setting-must-be-encrypted"
        ),
        "chat_auto_speak_initial_delay_seconds": 17,
        "chat_auto_speak_interval_seconds": 73,
        "chat_auto_speak_message_interval_seconds": 15,
        "chat_auto_speak_max_reply_chars": 499,
        "chat_timeout_seconds": 91.25,
        "chat_max_input_chars": 2345,
        "chat_history_turns": 8,
        "chat_history_max_chars": 13_579,
        "chat_group_recent_context_enabled": True,
        "chat_group_recent_context_messages": 17,
        "chat_group_recent_context_max_chars": 6789,
        "chat_group_recent_context_ttl_seconds": 421.5,
        "chat_memory_enabled": True,
        "chat_tagged_memory_enabled": False,
        "chat_memory_embedding_model": "memory-embedding-offline",
        "chat_memory_max_chars": 4567,
        "chat_memory_compression_max_tokens": 768,
        "chat_memory_compression_queue_capacity": 17,
        "chat_db_pool_size": 3,
        "chat_db_timeout_seconds": 13,
        "chat_max_sessions": 987,
        "chat_max_concurrent_requests": 7,
        "chat_cooldown_seconds": 3.25,
        "chat_chunk_chars": 777,
        "chat_max_reply_chars": 4321,
        "chat_multi_reply_enabled": True,
        "chat_multi_reply_max_messages": 4,
        "chat_multi_reply_interval_seconds": 0.5,
        "chat_multi_reply_prompt": "multi-reply-prompt-private-marker",
        "chat_queue_max_pending": 876,
        "chat_queue_workers": 6,
        "chat_queue_media_workers": 2,
        "chat_queue_max_attempts": 4,
        "chat_queue_retry_initial_seconds": 1.5,
        "chat_queue_retry_max_seconds": 18.5,
        "chat_queue_job_ttl_seconds": 1200,
        "chat_queue_result_ttl_seconds": 2400,
        "chat_naraka_timeout_seconds": 16.5,
        "chat_naraka_cooldown_seconds": 11.5,
        "chat_naraka_max_concurrent_requests": 3,
        "chat_allowed_groups": {"940000000000004"},
        "chat_allowed_users": {"940000000000005"},
        "chat_blocked_groups": {"940000000000007"},
        "chat_blocked_users": {"940000000000008"},
        "chat_allow_all": False,
        "chat_group_shared_session": True,
        "chat_priority": 71,
        "chat_reset_priority": 13,
        "chat_user_message_limit": 23,
        "chat_user_message_window_seconds": 1800,
        "chat_config_refresh_interval_seconds": 7.5,
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
    for field_name in EXPECTED_RUNTIME_FIELDS:
        assert getattr(loaded.config, field_name) == getattr(runtime_config, field_name)


def test_empty_system_prompt_persists_without_default_fallback(
    database_params: DatabaseParams, fake_mysql: FakeMySQL, runtime_config: RuntimeConfig,
) -> None:
    """保存空字符串后数据库仍是权威值，重新加载不能恢复默认提示词。"""
    store = _make_store(database_params)
    original = store.initialize(runtime_config)
    saved = store.save(
        runtime_config.model_copy(update={"chat_system_prompt": ""}),
        expected_revision=original.revision,
    )
    assert saved.config.chat_system_prompt == ""
    assert store.load().config.chat_system_prompt == ""


def test_existing_snapshot_without_chat_agent_steps_keeps_default_behavior(
    database_params: DatabaseParams, fake_mysql: FakeMySQL, runtime_config: RuntimeConfig,
) -> None:
    """老配置缺少轮次字段时使用已约定的五次默认预算，无需迁移。"""
    store = _make_store(database_params)
    store.initialize(runtime_config)
    fake_mysql.table[:] = [
        row for row in fake_mysql.table if row["config_key"] != "chat_agent_max_steps"
    ]

    loaded = store.load()

    assert loaded.config.chat_agent_max_steps == 5
    assert loaded.config.chat_web_search_max_steps == 3


def test_existing_snapshot_without_multi_reply_settings_keeps_feature_disabled(
    database_params: DatabaseParams, fake_mysql: FakeMySQL, runtime_config: RuntimeConfig,
) -> None:
    """既有数据库缺少新键时默认关闭，读取不改写配置或要求迁移表结构。"""
    store = _make_store(database_params)
    store.initialize(runtime_config)
    fake_mysql.table[:] = [
        row for row in fake_mysql.table if not row["config_key"].startswith("chat_multi_reply_")
    ]
    before = len(fake_mysql.table)
    loaded = store.load()
    assert loaded.config.chat_multi_reply_enabled is False
    assert loaded.config.chat_multi_reply_max_messages == 3
    assert loaded.config.chat_multi_reply_interval_seconds == 1
    assert loaded.revision == 1
    assert len(fake_mysql.table) == before


def test_message_trigger_snapshot_accepts_unused_sub_minute_interval(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    store = _make_store(database_params)
    store.initialize(runtime_config)
    replacements = {
        "chat_auto_speak_trigger_mode": '"message"',
        "chat_auto_speak_interval_seconds": "1",
    }
    for row in fake_mysql.table:
        if row["config_key"] in replacements:
            row["config_value"] = replacements[row["config_key"]]

    loaded = store.load()

    assert loaded.config.chat_auto_speak_trigger_mode == "message"
    assert loaded.config.chat_auto_speak_interval_seconds == 1


@pytest.mark.parametrize(
    ("field_name", "raw_text"),
    [
        (
            "chat_system_prompt",
            (
                "# 角色说明\n\n"
                "保留 `代码`、\"引号\" 和路径 `C:\\models`。\n\n"
                "```json\n{\"enabled\": true}\n```"
            ),
        ),
        ("chat_tts_reference_audio_path", "C:\\dispatch\\workflows"),
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
        if row["config_key"] == "chat_tts_token":
            row["config_value"] = raw_token
            break

    loaded = store.load()

    assert loaded.config.chat_tts_token is not None
    assert loaded.config.chat_tts_token.get_secret_value() == raw_token


def test_load_rejects_raw_text_fallback_for_non_string_setting(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    """非字符串配置不能借助 Pydantic 类型转换绕过 JSON 格式。"""

    store = _make_store(database_params)
    store.initialize(runtime_config)
    for row in fake_mysql.table:
        if row["config_key"] == "chat_web_search_enabled":
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
        if row["config_key"] == "chat_llm_max_tokens":
            row["config_value"] = str(invalid_value)
            break

    with pytest.raises(SettingsValidationError) as exc_info:
        store.load()

    reason = str(exc_info.value)
    assert reason == (
        "settings snapshot failed validation: "
        "chat_llm_max_tokens[greater_than_equal]"
    )
    assert str(invalid_value) not in reason


def test_uninitialized_store_rejects_load_and_save(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    store = SettingsStore(database_params, static_config=runtime_config)

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
    replacement = runtime_config.model_copy(update={"chat_model": "replacement"})

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
    updated_config = runtime_config.model_copy(update={"chat_model": "revision-two"})

    updated = store.save(updated_config, expected_revision=original.revision)

    assert updated == VersionedSettings(config=updated_config, revision=2)
    stale_candidate = runtime_config.model_copy(update={"chat_model": "stale-write"})
    with pytest.raises(SettingsRevisionConflictError):
        store.save(stale_candidate, expected_revision=original.revision)
    assert store.load() == updated


def test_save_returns_committed_snapshot_without_a_second_database_read(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
    monkeypatch,
) -> None:
    """提交成功后不能因验证性重读失败而误报失败并触发启动文件回滚。"""
    store = _make_store(database_params)
    store.initialize(runtime_config)
    candidate = runtime_config.model_copy(update={"chat_model": "committed-model"})
    monkeypatch.setattr(store, "load", Mock(side_effect=SettingsError("post-commit read failed")))
    assert store.save(candidate, expected_revision=1) == VersionedSettings(candidate, revision=2)
    assert store.load_revision() == 2


def test_load_revision_reads_only_current_version(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
) -> None:
    store = _make_store(database_params)
    store.initialize(runtime_config)

    assert store.load_revision() == 1

    updated = runtime_config.model_copy(update={"chat_model": "revision-two"})
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
            "chat_chunk_chars": 5000,
            "chat_max_reply_chars": 100,
        }
    )

    with pytest.raises(SettingsValidationError):
        store.save(invalid_candidate, expected_revision=original.revision)

    assert store.load() == original


@pytest.mark.parametrize("legacy_key, legacy_value", [
    ("chat_ai_base_url", '"stale-value"'),
    ("chat_agent_enabled", "false"),
])
def test_load_drops_legacy_keys_removed_from_model(
    database_params: DatabaseParams,
    fake_mysql: FakeMySQL,
    runtime_config: RuntimeConfig,
    legacy_key: str,
    legacy_value: str,
) -> None:
    """数据库快照中残留已删除字段时，load() 必须丢弃而非拒绝，与 extra="ignore" 一致。"""

    store = _make_store(database_params)
    original = store.initialize(runtime_config)

    # 模拟历史版本写入、当前模型已移除的字段。
    fake_mysql.table.append(
        {
            "config_key": legacy_key,
            "config_value": legacy_value,
            "description": "",
        }
    )

    loaded = store.load()
    assert loaded == original
    plain = runtime_config_to_plain_values(loaded.config)
    assert legacy_key not in plain
    saved = store.save(loaded.config, expected_revision=loaded.revision)
    assert saved.revision == 2
    assert all(row["config_key"] != legacy_key for row in fake_mysql.table)


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
    fake_mysql.table_columns["chat_setting"] = ["id", "config_key"]
    store = _make_store(database_params)

    with pytest.raises(SettingsSchemaError, match="schema is invalid"):
        store.load()


def test_runtime_config_storage_contract_covers_all_plugin_and_nonebot_fields() -> None:
    """完整字段包含插件、NoneBot、OneBot 和仅供桌面读取的启动字段。"""
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
        | {"app_enabled_plugins", "agent_bot_napcat_mode", "agent_bot_napcat_webui_port"}
    )


def test_runtime_config_to_plain_values_unpacks_secretstr_and_collections() -> None:
    config = RuntimeConfig(
        onebot_v11_access_token="x" * 40,
        chat_session_secret="y" * 40,
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
    store = SettingsStore(database_params, static_config=runtime_config)
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
