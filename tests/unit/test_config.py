import pytest
from nonebot.config import BaseSettings
from pydantic import ValidationError

from agent_bot.config import PluginConfig, RuntimeConfig


def make_config(**overrides: object) -> PluginConfig:
    return PluginConfig(
        onebot_v11_access_token="test-onebot-access-token-with-enough-entropy",
        chat_session_secret="test-session-secret-with-enough-entropy",
        **overrides,
    )


def make_enabled_auto_speak_config(**overrides: object) -> PluginConfig:
    """Build an enabled group-only auto-speak configuration for focused tests."""

    values: dict[str, object] = {
        "chat_auto_speak_enabled": True,
        "chat_auto_speak_bot_id": "90001",
        "chat_auto_speak_groups": "20001",
        "chat_mcp_enabled": True,
        "chat_auto_speak_mcp_code": "decision",
        "chat_group_recent_context_enabled": True,
    }
    values.update(overrides)
    return make_config(**values)


def test_config_has_safe_operational_defaults() -> None:
    config = make_config()

    assert config.chat_model == "Qwen/Qwen3-8B"
    assert config.chat_vision_model == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert config.chat_image_model == "gpt-image-1"
    assert config.chat_image_model != config.chat_vision_model
    assert config.chat_history_turns == 6
    assert config.chat_history_ttl_seconds == 604_800
    assert config.chat_group_recent_context_enabled is False
    assert config.chat_group_recent_context_messages == 10
    assert config.chat_group_recent_context_max_chars == 4000
    assert config.chat_group_recent_context_ttl_seconds == 604_800
    assert config.chat_memory_enabled is True
    assert config.chat_tagged_memory_enabled is True
    assert config.chat_memory_max_chars == 4000
    assert config.chat_memory_compression_max_tokens == 512
    assert config.chat_memory_compression_queue_capacity == 64
    assert config.chat_db_pool_size == 8
    assert config.chat_db_timeout_seconds == 10
    assert config.chat_allowed_groups == set()
    assert config.chat_allowed_users == set()
    assert config.chat_allow_all is False
    assert config.chat_naraka_cookie is None
    assert config.chat_naraka_heybox_id is None
    assert config.chat_naraka_default_role_id is None
    assert config.chat_naraka_server == "163"
    assert config.chat_naraka_season == "qianji"
    assert config.chat_naraka_battle_tid == "5000001"
    assert config.chat_auto_speak_enabled is False
    assert config.chat_auto_speak_bot_id is None
    assert config.chat_auto_speak_groups == set()
    assert config.chat_auto_speak_trigger_mode == "interval"
    assert config.chat_config_refresh_interval_seconds == 5
    assert config.chat_llm_max_tokens is None
    assert config.chat_llm_temperature is None
    assert config.chat_llm_top_p is None
    assert config.chat_llm_top_k is None
    assert config.chat_llm_min_p is None
    assert config.chat_llm_presence_penalty is None
    assert config.chat_llm_frequency_penalty is None
    assert config.chat_llm_repetition_penalty is None
    assert config.chat_llm_stop is None
    assert config.chat_llm_enable_thinking is None
    assert config.chat_llm_forward_user_name is False
    assert config.chat_agent_enabled is True
    assert config.chat_agent_max_steps is None
    assert config.chat_mcp_services == []
    assert config.chat_web_search_enabled is True
    assert config.chat_web_search_max_steps == 3
    assert config.chat_image_recognition_enabled is True
    assert config.chat_max_images_per_message == 4
    assert config.chat_image_generation_enabled is False
    assert config.chat_image_timeout_seconds == 60
    assert config.chat_image_response_format == "png"
    assert config.chat_image_max_bytes == 10_485_760
    assert config.chat_local_workflow_enabled is False
    assert config.chat_local_workflow_base_url == "http://127.0.0.1:8188"
    assert config.chat_local_workflow_directory is None
    assert config.chat_local_workflow_default_key == "sdxl-lightning-4step"
    assert config.chat_local_workflow_timeout_seconds == 500
    assert config.chat_keyword_at_replies == {}
    assert config.chat_auto_mention_enabled is True
    assert config.chat_mention_tool_enabled is True
    assert config.chat_tts_enabled is False
    assert config.chat_tts_url is None
    assert config.chat_tts_token is None
    assert config.chat_tts_model == "tts-1"
    assert config.chat_tts_voice == "alloy"
    assert config.chat_tts_response_format == "mp3"
    assert config.chat_tts_speed == 1
    assert config.chat_tts_timeout_seconds == 30
    assert config.chat_tts_max_input_chars == 1000
    assert config.chat_tts_max_audio_bytes == 5_242_880
    assert config.chat_queue_max_pending == 1000
    assert config.chat_queue_workers == 4
    assert config.chat_queue_media_workers == 1
    assert config.chat_queue_max_attempts == 3
    assert config.chat_queue_retry_initial_seconds == 2
    assert config.chat_queue_retry_max_seconds == 30
    assert config.chat_queue_job_ttl_seconds == 900
    assert config.chat_queue_result_ttl_seconds == 1800
    assert config.chat_timeout_seconds == 300


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chat_queue_max_pending", 0),
        ("chat_queue_max_pending", 100_001),
        ("chat_queue_workers", 0),
        ("chat_queue_workers", 65),
        ("chat_queue_media_workers", 0),
        ("chat_queue_media_workers", 17),
        ("chat_memory_compression_queue_capacity", 0),
        ("chat_memory_compression_queue_capacity", 1001),
        ("chat_db_pool_size", 0),
        ("chat_db_pool_size", 65),
        ("chat_db_timeout_seconds", 0),
        ("chat_db_timeout_seconds", 61),
        ("chat_queue_max_attempts", 0),
        ("chat_queue_max_attempts", 11),
        ("chat_queue_retry_initial_seconds", 0),
        ("chat_queue_retry_max_seconds", 301),
        ("chat_queue_job_ttl_seconds", 59),
        ("chat_queue_result_ttl_seconds", 59),
    ],
)
def test_queue_rejects_unsafe_limits(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_config(**{field: value})


def test_queue_retry_maximum_cannot_be_less_than_initial_delay() -> None:
    with pytest.raises(ValidationError, match="retry"):
        make_config(
            chat_queue_retry_initial_seconds=20,
            chat_queue_retry_max_seconds=10,
        )


def test_queue_result_ttl_covers_the_full_job_lifetime() -> None:
    with pytest.raises(ValidationError, match="result TTL"):
        make_config(
            chat_queue_job_ttl_seconds=600,
            chat_queue_result_ttl_seconds=599,
        )


def test_media_generation_does_not_require_legacy_tts_url() -> None:
    enabled_without_legacy_endpoint = make_config(chat_tts_enabled=True)
    assert enabled_without_legacy_endpoint.chat_tts_url is None

    config = make_config(
        chat_tts_enabled=True,
        chat_tts_url="https://tts.example.invalid/v1/audio/speech",
        chat_tts_token="tts-private-token-marker",
        chat_tts_model="speech-model",
        chat_tts_voice="friendly-voice",
        chat_tts_response_format="opus",
        chat_tts_speed=1.25,
    )

    assert config.chat_tts_url == "https://tts.example.invalid/v1/audio/speech"
    assert config.chat_tts_response_format == "opus"
    assert config.chat_tts_speed == 1.25
    assert "tts-private-token-marker" not in repr(config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chat_image_timeout_seconds", 0),
        ("chat_image_timeout_seconds", 121),
        ("chat_image_max_bytes", 1023),
        ("chat_image_max_bytes", 10_485_761),
        ("chat_image_response_format", "gif"),
    ],
)
def test_image_generation_rejects_unsafe_limits(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_config(**{field: value})


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/comfy",
        "https://user:password@comfy.example.invalid",
        "https://comfy.example.invalid/?token=secret",
        "https://comfy.example.invalid/#fragment",
    ],
)
def test_local_workflow_rejects_unsafe_comfy_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        make_config(chat_local_workflow_base_url=url)


def test_enabled_local_workflow_requires_image_generation_and_mcp() -> None:
    with pytest.raises(ValidationError, match="image generation"):
        make_config(
            chat_local_workflow_enabled=True,
            chat_mcp_enabled=True,
        )
    with pytest.raises(ValidationError, match="MCP"):
        make_config(
            chat_local_workflow_enabled=True,
            chat_image_generation_enabled=True,
        )

    config = make_config(
        chat_local_workflow_enabled=True,
        chat_image_generation_enabled=True,
        chat_mcp_enabled=True,
    )
    assert config.chat_local_workflow_enabled is True


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/speech",
        "https://user:password@tts.example.invalid/v1/audio/speech",
        "https://tts.example.invalid/v1/audio/speech?token=secret",
        "https://tts.example.invalid/v1/audio/speech#fragment",
        "https://tts.example.invalid/v1/audio/\nspeech",
    ],
)
def test_tts_rejects_unsafe_endpoint_urls(url: str) -> None:
    with pytest.raises(ValidationError):
        make_config(chat_tts_url=url)


def test_keyword_at_replies_are_trimmed_and_accept_safe_text() -> None:
    config = make_config(
        chat_keyword_at_replies={
            "  在吗  ": "  我在。  ",
            "报时": "现在还不能报时。\n请稍后再试。",
        }
    )

    assert config.chat_keyword_at_replies == {
        "在吗": "我在。",
        "报时": "现在还不能报时。\n请稍后再试。",
    }


@pytest.mark.parametrize(
    "rules",
    [
        {"": "回复"},
        {"关键词": ""},
        {"x" * 65: "回复"},
        {"关键词": "x" * 1001},
        {"坏\0关键词": "回复"},
        {"关键词": "坏\0回复"},
        {"\uff21\uff22\uff23": "一", "abc": "二"},
        {str(index): "回复" for index in range(51)},
    ],
)
def test_keyword_at_replies_reject_invalid_or_ambiguous_rules(
    rules: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        make_config(chat_keyword_at_replies=rules)


def test_llm_request_options_accept_supported_boundaries() -> None:
    config = make_config(
        chat_llm_max_tokens=128_000,
        chat_llm_temperature=2,
        chat_llm_top_p=1,
        chat_llm_top_k=100,
        chat_llm_min_p=0,
        chat_llm_presence_penalty=-2,
        chat_llm_frequency_penalty=2,
        chat_llm_repetition_penalty=1,
        chat_llm_stop=["END", "STOP"],
        chat_llm_enable_thinking=True,
        chat_llm_forward_user_name=True,
    )

    assert config.chat_llm_max_tokens == 128_000
    assert config.chat_llm_temperature == 2
    assert config.chat_llm_top_p == 1
    assert config.chat_llm_top_k == 100
    assert config.chat_llm_min_p == 0
    assert config.chat_llm_presence_penalty == -2
    assert config.chat_llm_frequency_penalty == 2
    assert config.chat_llm_repetition_penalty == 1
    assert config.chat_llm_stop == ["END", "STOP"]
    assert config.chat_llm_enable_thinking is True
    assert config.chat_llm_forward_user_name is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chat_llm_max_tokens", 0),
        ("chat_llm_max_tokens", 128_001),
        ("chat_llm_temperature", -0.01),
        ("chat_llm_temperature", 2.01),
        ("chat_llm_temperature", float("nan")),
        ("chat_llm_top_p", -0.01),
        ("chat_llm_top_p", 1.01),
        ("chat_llm_top_k", 0),
        ("chat_llm_top_k", 101),
        ("chat_llm_min_p", -0.01),
        ("chat_llm_min_p", 1.01),
        ("chat_llm_presence_penalty", -2.01),
        ("chat_llm_presence_penalty", 2.01),
        ("chat_llm_frequency_penalty", -2.01),
        ("chat_llm_frequency_penalty", 2.01),
        ("chat_llm_repetition_penalty", 0.99),
        ("chat_llm_repetition_penalty", 2.01),
    ],
)
def test_llm_request_options_reject_invalid_bounds(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_config(**{field: value})


@pytest.mark.parametrize(
    "stop",
    [
        [],
        [""],
        ["   "],
        ["x" * 257],
        ["1", "2", "3", "4", "5"],
    ],
)
def test_llm_stop_sequences_reject_invalid_values(stop: list[str]) -> None:
    with pytest.raises(ValidationError):
        make_config(chat_llm_stop=stop)


@pytest.mark.parametrize("field_name", [
    "chat_agent_max_steps", "chat_web_search_max_steps",
])
@pytest.mark.parametrize("max_steps", [0, 6])
def test_config_rejects_unbounded_agent_steps(field_name: str, max_steps: int) -> None:
    """普通聊天和联网工具遵守相同的轮次上限。"""
    with pytest.raises(ValidationError):
        make_config(**{field_name: max_steps})


@pytest.mark.parametrize("max_steps", [None, 1, 5])
def test_chat_agent_accepts_optional_bounded_steps(max_steps: int | None) -> None:
    """可清空覆盖值，也可选择范围内的推理轮次。"""
    assert make_config(chat_agent_max_steps=max_steps).chat_agent_max_steps == (
        max_steps
    )


def test_general_mcp_services_accept_bounded_stable_codes() -> None:
    """普通 Agent 只接受可安全登记到 ai_mcp 的稳定服务 code。"""

    config = make_config(
        chat_mcp_services=["windows-mcp", "internal_docs"],
    )

    assert config.chat_mcp_services == ["windows-mcp", "internal_docs"]


@pytest.mark.parametrize(
    "services",
    [
        ["laya"],
        ["windows-mcp", "windows-mcp"],
        ["HTTP://untrusted.test/mcp"],
        ["windows-mcp", "docs", "files", "fourth"],
    ],
)
def test_general_mcp_services_reject_reserved_or_unbounded_values(
    services: list[str],
) -> None:
    """配置不能使用保留的决策服务名，也不能超过三服务上限。"""

    with pytest.raises(ValidationError):
        make_config(chat_mcp_services=services)




@pytest.mark.parametrize("max_images", [0, 9])
def test_image_recognition_rejects_unbounded_attachment_counts(max_images: int) -> None:
    with pytest.raises(ValidationError):
        make_config(chat_max_images_per_message=max_images)


def test_config_repr_redacts_prompt_and_inbound_qq_allowlists() -> None:
    prompt = "private supplemental persona prompt"
    allowed_user = "940000000000010"
    allowed_group = "940000000000011"
    keyword_marker = "private-keyword-rule-marker"

    rendered = repr(
        make_config(
            chat_system_prompt=prompt,
            chat_allowed_users={allowed_user},
            chat_allowed_groups={allowed_group},
            chat_keyword_at_replies={keyword_marker: "private reply marker"},
        )
    )

    assert prompt not in rendered
    assert allowed_user not in rendered
    assert allowed_group not in rendered
    assert keyword_marker not in rendered


@pytest.mark.parametrize(
    "overrides",
    [
        {"chat_auto_speak_groups": "20001"},
    ],
)
def test_enabled_auto_speak_requires_bot_id(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="bot"):
        make_config(chat_auto_speak_enabled=True, **overrides)


def test_enabled_auto_speak_requires_at_least_one_group() -> None:
    with pytest.raises(ValidationError, match="group"):
        make_config(
            chat_auto_speak_enabled=True,
            chat_auto_speak_bot_id="90001",
            chat_mcp_enabled=True,
            chat_auto_speak_mcp_code="decision",
            chat_group_recent_context_enabled=True,
        )


@pytest.mark.parametrize("mode", ["timer", "MESSAGE", ""])
def test_auto_speak_rejects_unknown_trigger_mode(mode: str) -> None:
    with pytest.raises(ValidationError):
        make_config(chat_auto_speak_trigger_mode=mode)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"chat_auto_speak_mcp_code": None}, "MCP"),
        ({"chat_group_recent_context_enabled": False}, "recent group context"),
    ],
)
def test_enabled_auto_speak_requires_decision_dependencies(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_enabled_auto_speak_config(**overrides)




@pytest.mark.parametrize(
    ("field", "raw_value", "expected"),
    [
        ("chat_auto_speak_groups", 20001, {"20001"}),
        ("chat_auto_speak_groups", "20001", {"20001"}),
        (
            "chat_auto_speak_groups",
            '["20001", "20002"]',
            {"20001", "20002"},
        ),
        (
            "chat_auto_speak_groups",
            "20001, 20002,20001",
            {"20001", "20002"},
        ),
        (
            "chat_auto_speak_groups",
            ["20001", 20002],
            {"20001", "20002"},
        ),
    ],
)
def test_auto_speak_targets_accept_nonebot_environment_shapes(
    field: str,
    raw_value: object,
    expected: set[str],
) -> None:
    config = make_config(
        chat_auto_speak_bot_id=90001,
        **{field: raw_value},
    )

    assert config.chat_auto_speak_bot_id is not None
    assert config.chat_auto_speak_bot_id.get_secret_value() == "90001"
    assert getattr(config, field) == expected


def test_auto_speak_targets_accept_real_nonebot_environment_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_AUTO_SPEAK_ENABLED", "true")
    monkeypatch.setenv("CHAT_AUTO_SPEAK_BOT_ID", "90001")
    monkeypatch.setenv("CHAT_AUTO_SPEAK_GROUPS", '["20001", 20002]')
    monkeypatch.setenv("CHAT_MCP_ENABLED", "true")
    monkeypatch.setenv("CHAT_AUTO_SPEAK_MCP_CODE", "decision")
    monkeypatch.setenv("CHAT_GROUP_RECENT_CONTEXT_ENABLED", "true")

    values = BaseSettings._settings_build_values(
        PluginConfig,
        {
            "onebot_v11_access_token": "test-onebot-access-token-with-enough-entropy",
            "chat_session_secret": "test-session-secret-with-enough-entropy",
        },
        env_file=None,
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )
    config = PluginConfig.model_validate(values)

    assert config.chat_auto_speak_groups == {"20001", "20002"}


def test_blank_auto_speak_values_disable_feature_through_nonebot_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CHAT_AUTO_SPEAK_ENABLED", "false")
    monkeypatch.setenv("CHAT_AUTO_SPEAK_BOT_ID", "")
    monkeypatch.setenv("CHAT_AUTO_SPEAK_GROUPS", "")

    values = BaseSettings._settings_build_values(
        PluginConfig,
        {
            "onebot_v11_access_token": "test-onebot-access-token-with-enough-entropy",
            "chat_session_secret": "test-session-secret-with-enough-entropy",
        },
        env_file=None,
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )
    config = PluginConfig.model_validate(values)

    assert config.chat_auto_speak_bot_id is None
    assert config.chat_auto_speak_groups == set()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chat_auto_speak_bot_id", "1234"),
        ("chat_auto_speak_bot_id", "01234"),
        ("chat_auto_speak_bot_id", "123456789012345678901"),
        ("chat_auto_speak_bot_id", "\uff11\uff12\uff13\uff14\uff15"),
        ("chat_auto_speak_groups", "1234"),
        ("chat_auto_speak_groups", "01234"),
        ("chat_auto_speak_groups", "123456789012345678901"),
        ("chat_auto_speak_groups", "group-20001"),
    ],
)
def test_auto_speak_rejects_invalid_qq_identifiers(field: str, value: object) -> None:
    base: dict[str, object] = {
        "chat_auto_speak_enabled": True,
        "chat_auto_speak_bot_id": "90001",
        "chat_auto_speak_groups": "20001",
    }
    base[field] = value

    with pytest.raises(ValidationError):
        make_config(**base)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chat_auto_speak_initial_delay_seconds", 9.99),
        ("chat_auto_speak_interval_seconds", 59.99),
        ("chat_auto_speak_prompt", ""),
        ("chat_auto_speak_prompt", "x" * 1001),
        ("chat_auto_speak_laya_prompt", ""),
        ("chat_auto_speak_laya_prompt", "x" * 1001),
        ("chat_auto_speak_max_reply_chars", 0),
        ("chat_auto_speak_max_reply_chars", 1001),
    ],
)
def test_auto_speak_rejects_invalid_bounds(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_config(**{field: value})


def test_auto_speak_accepts_identifier_and_length_boundaries() -> None:
    config = make_enabled_auto_speak_config(
        chat_auto_speak_bot_id="12345",
        chat_auto_speak_groups=["23456", "12345678901234567890"],
        chat_auto_speak_prompt="x" * 1000,
        chat_auto_speak_laya_prompt="y" * 1000,
        chat_auto_speak_initial_delay_seconds=10,
        chat_auto_speak_interval_seconds=60,
        chat_auto_speak_max_reply_chars=1000,
    )

    assert config.chat_auto_speak_groups == {
        "23456",
        "12345678901234567890",
    }


def test_message_trigger_auto_speak_accepts_unused_sub_minute_interval() -> None:
    config = make_enabled_auto_speak_config(
        chat_auto_speak_trigger_mode="message",
        chat_auto_speak_interval_seconds=1,
    )

    assert config.chat_auto_speak_interval_seconds == 1


def test_auto_speak_rejects_more_than_twenty_targets() -> None:
    with pytest.raises(ValidationError, match=r"20|target"):
        make_config(
            chat_auto_speak_enabled=True,
            chat_auto_speak_bot_id="90001",
            chat_auto_speak_groups={str(value) for value in range(10000, 10021)},
        )


def test_auto_speak_reserves_one_ai_concurrency_slot_for_interactive_chat() -> None:
    with pytest.raises(ValidationError, match=r"concurrent|interactive"):
        make_enabled_auto_speak_config(
            chat_max_concurrent_requests=1,
        )


def test_auto_speak_sensitive_configuration_is_hidden_from_repr() -> None:
    sensitive_values = {
        "bot": "90001",
        "group": "20001",
        "user": "30001",
        "prompt": "private scheduled announcement prompt",
        "laya_prompt": "private scheduled Laya decision prompt",
    }
    config = make_enabled_auto_speak_config(
        chat_auto_speak_bot_id=sensitive_values["bot"],
        chat_auto_speak_groups=sensitive_values["group"],
        chat_auto_speak_prompt=sensitive_values["prompt"],
        chat_auto_speak_laya_prompt=sensitive_values["laya_prompt"],
    )

    rendered = repr(config)
    assert all(value not in rendered for value in sensitive_values.values())


def test_auto_speak_validation_errors_hide_sensitive_inputs() -> None:
    sensitive_values = {
        "bot": "private-invalid-bot-id",
        "group": "private-invalid-group-id",
        "user": "private-invalid-user-id",
        "prompt": "private-invalid-prompt-" * 100,
        "laya_prompt": "private-invalid-laya-prompt-" * 100,
    }

    with pytest.raises(ValidationError) as exc_info:
        make_config(
            chat_auto_speak_enabled=True,
            chat_auto_speak_bot_id=sensitive_values["bot"],
            chat_auto_speak_groups=sensitive_values["group"],
            chat_auto_speak_prompt=sensitive_values["prompt"],
            chat_auto_speak_laya_prompt=sensitive_values["laya_prompt"],
        )

    rendered = str(exc_info.value)
    assert all(value not in rendered for value in sensitive_values.values())


def test_config_requires_session_hmac_secret() -> None:
    with pytest.raises(ValidationError):
        PluginConfig()


def test_config_requires_strong_onebot_access_token() -> None:
    with pytest.raises(ValidationError, match="onebot_v11_access_token"):
        PluginConfig(
            onebot_v11_access_token="replace-me",
            chat_session_secret="test-session-secret-with-enough-entropy",
        )




@pytest.mark.parametrize(
    ("field", "value"),
    [
        (
            "onebot_v11_ws_urls",
            {"wss://private-user:private-password@example.invalid/ws"},
        ),
        (
            "onebot_v11_api_roots",
            {"bot": "https://private-user:private-password@example.invalid/api"},
        ),
        (
            "onebot_v11_ws_urls",
            {"wss://example.invalid/ws?token=private-query-token"},
        ),
        (
            "onebot_v11_api_roots",
            {"bot": "https://example.invalid/api#private-fragment"},
        ),
    ],
)
def test_runtime_config_rejects_unsafe_onebot_url_metadata(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        RuntimeConfig(
            onebot_v11_access_token="test-onebot-access-token-with-enough-entropy",
            chat_session_secret="test-session-secret-with-enough-entropy",
            **{field: value},
        )

    rendered = str(exc_info.value)
    assert "private-user" not in rendered
    assert "private-password" not in rendered
    assert "private-query-token" not in rendered
    assert "private-fragment" not in rendered


def test_runtime_config_accepts_large_persistent_memory_windows() -> None:
    config = RuntimeConfig(
        onebot_v11_access_token="test-onebot-access-token-with-enough-entropy",
        chat_session_secret="test-session-secret-with-enough-entropy",
        chat_history_turns=1000,
        chat_group_recent_context_messages=500,
    )

    assert config.chat_history_turns == 1000
    assert config.chat_group_recent_context_messages == 500


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chat_timeout_seconds", 0),
        ("chat_max_input_chars", 0),
        ("chat_history_turns", -1),
        ("chat_group_recent_context_messages", 0),
        ("chat_group_recent_context_max_chars", 255),
        ("chat_group_recent_context_max_chars", 12_001),
        ("chat_group_recent_context_ttl_seconds", 9),
        ("chat_memory_max_chars", 255),
        ("chat_memory_max_chars", 8001),
        ("chat_memory_compression_max_tokens", 63),
        ("chat_memory_compression_max_tokens", 4097),
        ("chat_history_ttl_seconds", 0),
        ("chat_max_sessions", 0),
        ("chat_max_concurrent_requests", 0),
        ("chat_chunk_chars", 0),
        ("chat_max_reply_chars", 0),
        ("chat_cooldown_seconds", -0.1),
        ("chat_naraka_timeout_seconds", 0),
        ("chat_naraka_cooldown_seconds", -0.1),
        ("chat_naraka_max_concurrent_requests", 0),
        ("chat_config_refresh_interval_seconds", 0.5),
        ("chat_config_refresh_interval_seconds", 301),
        ("chat_system_prompt", ""),
        ("chat_system_prompt", "x" * 20_001),
        ("chat_vision_model", ""),
        ("chat_vision_model", "x" * 256),
        ("chat_vision_model", "vision\0model"),
        ("chat_image_model", ""),
        ("chat_image_model", "x" * 256),
        ("chat_image_model", "image\0model"),
    ],
)
def test_config_rejects_invalid_bounds(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_config(**{field: value})


def test_config_rejects_chunk_larger_than_total_reply() -> None:
    with pytest.raises(ValidationError, match="chunk"):
        make_config(
            chat_chunk_chars=200,
            chat_max_reply_chars=100,
        )


def test_config_rejects_reused_secrets_and_unsafe_priorities() -> None:
    shared_secret = "shared-secret-that-is-long-enough-for-validation"
    with pytest.raises(ValidationError, match="must be different"):
        PluginConfig(
            onebot_v11_access_token=shared_secret,
            chat_session_secret=shared_secret,
        )

    with pytest.raises(ValidationError, match="reset priority"):
        make_config(
            chat_priority=50,
            chat_reset_priority=50,
        )


def test_config_token_is_hidden_from_repr() -> None:
    config = make_config(
        chat_naraka_default_role_id="top-secret",
        chat_tts_token="tts-top-secret",
    )

    assert "top-secret" not in repr(config)
    assert "tts-top-secret" not in repr(config)
    assert "test-session-secret" not in repr(config)
    assert "test-onebot-access-token" not in repr(config)


def test_config_validation_errors_hide_secret_inputs() -> None:
    token = "sensitive-onebot-token-that-must-never-be-logged"

    with pytest.raises(ValidationError) as exc_info:
        PluginConfig(onebot_v11_access_token=token)

    assert token not in str(exc_info.value)


def test_naraka_credentials_must_be_configured_together() -> None:
    with pytest.raises(ValidationError, match="Naraka credentials"):
        make_config(chat_naraka_cookie="pkey=secret")

    with pytest.raises(ValidationError, match="Naraka credentials"):
        make_config(chat_naraka_heybox_id="123456")


def test_blank_optional_naraka_settings_disable_feature() -> None:
    config = make_config(
        chat_naraka_cookie="",
        chat_naraka_heybox_id="",
        chat_naraka_default_role_id="",
    )

    assert config.chat_naraka_cookie is None
    assert config.chat_naraka_heybox_id is None
    assert config.chat_naraka_default_role_id is None


def test_numeric_naraka_query_settings_are_normalized_to_strings() -> None:
    config = make_config(
        chat_naraka_server=163,
        chat_naraka_battle_tid=5000001,
    )

    assert config.chat_naraka_server == "163"
    assert config.chat_naraka_battle_tid == "5000001"


def test_numeric_naraka_account_and_role_ids_are_normalized_to_secrets() -> None:
    config = make_config(
        chat_naraka_cookie=(
            "pkey=fake; x_heybox_id=123456; x_pkey=fake; x_xhh_tokenid=fake-token"
        ),
        chat_naraka_heybox_id=123456,
        chat_naraka_default_role_id=123456,
    )

    assert config.chat_naraka_heybox_id is not None
    assert config.chat_naraka_heybox_id.get_secret_value() == "123456"
    assert config.chat_naraka_default_role_id is not None
    assert config.chat_naraka_default_role_id.get_secret_value() == "123456"


def test_naraka_credentials_and_default_role_are_hidden() -> None:
    config = make_config(
        chat_naraka_cookie=(
            "pkey=fake; x_heybox_id=123456; x_pkey=fake; x_xhh_tokenid=fake-token"
        ),
        chat_naraka_heybox_id="123456",
        chat_naraka_default_role_id="private_role_123",
    )

    rendered = repr(config)
    assert "fake-token" not in rendered
    assert "123456" not in rendered
    assert "private_role_123" not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chat_naraka_heybox_id", "not-a-number"),
        ("chat_naraka_default_role_id", "https://attacker.invalid"),
        ("chat_naraka_server", "../server"),
        ("chat_naraka_season", "bad season"),
        ("chat_naraka_battle_tid", ""),
    ],
)
def test_config_rejects_invalid_naraka_identifiers(field: str, value: str) -> None:
    base: dict[str, object] = {
        "chat_naraka_cookie": (
            "pkey=fake; x_heybox_id=123456; x_pkey=fake; x_xhh_tokenid=fake-token"
        ),
        "chat_naraka_heybox_id": "123456",
    }
    base[field] = value

    with pytest.raises(ValidationError):
        make_config(**base)
