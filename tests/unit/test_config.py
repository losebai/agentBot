import pytest
from nonebot.config import BaseSettings
from pydantic import ValidationError

from agent_bot.config import PluginConfig, RuntimeConfig


def make_config(**overrides: object) -> PluginConfig:
    return PluginConfig(
        onebot_v11_access_token="test-onebot-access-token-with-enough-entropy",
        dispatch_chat_session_secret="test-session-secret-with-enough-entropy",
        **overrides,
    )


def make_enabled_auto_speak_config(**overrides: object) -> PluginConfig:
    """Build an enabled group-only auto-speak configuration for focused tests."""

    values: dict[str, object] = {
        "dispatch_chat_auto_speak_enabled": True,
        "dispatch_chat_auto_speak_bot_id": "90001",
        "dispatch_chat_auto_speak_groups": "20001",
        "dispatch_chat_mcp_enabled": True,
        "dispatch_chat_laya_enabled": True,
        "dispatch_chat_group_recent_context_enabled": True,
    }
    values.update(overrides)
    return make_config(**values)


def test_config_has_safe_operational_defaults() -> None:
    config = make_config()

    assert config.dispatch_chat_ai_base_url == "http://127.0.0.1:8090/dispatch-ai"
    assert config.dispatch_chat_model == "Qwen/Qwen3-8B"
    assert config.dispatch_chat_vision_model == "Qwen/Qwen2.5-VL-7B-Instruct"
    assert config.dispatch_chat_image_model == "gpt-image-1"
    assert config.dispatch_chat_image_model != config.dispatch_chat_vision_model
    assert config.dispatch_chat_history_turns == 6
    assert config.dispatch_chat_history_ttl_seconds == 604_800
    assert config.dispatch_chat_group_recent_context_enabled is False
    assert config.dispatch_chat_group_recent_context_messages == 10
    assert config.dispatch_chat_group_recent_context_max_chars == 4000
    assert config.dispatch_chat_group_recent_context_ttl_seconds == 604_800
    assert config.dispatch_chat_memory_enabled is True
    assert config.dispatch_chat_tagged_memory_enabled is True
    assert config.dispatch_chat_memory_max_chars == 4000
    assert config.dispatch_chat_memory_compression_max_tokens == 512
    assert config.dispatch_chat_memory_compression_queue_capacity == 64
    assert config.dispatch_chat_db_pool_size == 8
    assert config.dispatch_chat_db_timeout_seconds == 10
    assert config.dispatch_chat_model_cache_ttl_seconds == 5
    assert config.dispatch_chat_allowed_groups == set()
    assert config.dispatch_chat_allowed_users == set()
    assert config.dispatch_chat_ai_token is None
    assert config.dispatch_chat_allow_all is False
    assert config.dispatch_chat_naraka_cookie is None
    assert config.dispatch_chat_naraka_heybox_id is None
    assert config.dispatch_chat_naraka_default_role_id is None
    assert config.dispatch_chat_naraka_server == "163"
    assert config.dispatch_chat_naraka_season == "qianji"
    assert config.dispatch_chat_naraka_battle_tid == "5000001"
    assert config.dispatch_chat_auto_speak_enabled is False
    assert config.dispatch_chat_auto_speak_bot_id is None
    assert config.dispatch_chat_auto_speak_groups == set()
    assert config.dispatch_chat_auto_speak_users == set()
    assert config.dispatch_chat_auto_speak_trigger_mode == "interval"
    assert config.dispatch_chat_config_refresh_interval_seconds == 5
    assert config.dispatch_chat_llm_max_tokens is None
    assert config.dispatch_chat_llm_temperature is None
    assert config.dispatch_chat_llm_top_p is None
    assert config.dispatch_chat_llm_top_k is None
    assert config.dispatch_chat_llm_min_p is None
    assert config.dispatch_chat_llm_presence_penalty is None
    assert config.dispatch_chat_llm_frequency_penalty is None
    assert config.dispatch_chat_llm_repetition_penalty is None
    assert config.dispatch_chat_llm_stop is None
    assert config.dispatch_chat_llm_enable_thinking is None
    assert config.dispatch_chat_llm_forward_user_name is False
    assert config.dispatch_chat_agent_enabled is True
    assert config.dispatch_chat_agent_max_steps is None
    assert config.dispatch_chat_mcp_services == []
    assert config.dispatch_chat_web_search_enabled is True
    assert config.dispatch_chat_web_search_max_steps == 3
    assert config.dispatch_chat_image_recognition_enabled is True
    assert config.dispatch_chat_max_images_per_message == 4
    assert config.dispatch_chat_image_generation_enabled is False
    assert config.dispatch_chat_image_timeout_seconds == 60
    assert config.dispatch_chat_image_response_format == "png"
    assert config.dispatch_chat_image_max_bytes == 10_485_760
    assert config.dispatch_chat_local_workflow_enabled is False
    assert config.dispatch_chat_local_workflow_base_url == "http://127.0.0.1:8188"
    assert config.dispatch_chat_local_workflow_directory is None
    assert config.dispatch_chat_local_workflow_default_key == "sdxl-lightning-4step"
    assert config.dispatch_chat_local_workflow_timeout_seconds == 500
    assert config.dispatch_chat_keyword_at_replies == {}
    assert config.dispatch_chat_auto_mention_enabled is True
    assert config.dispatch_chat_mention_tool_enabled is True
    assert config.dispatch_chat_tts_enabled is False
    assert config.dispatch_chat_tts_url is None
    assert config.dispatch_chat_tts_token is None
    assert config.dispatch_chat_tts_model == "tts-1"
    assert config.dispatch_chat_tts_voice == "alloy"
    assert config.dispatch_chat_tts_response_format == "mp3"
    assert config.dispatch_chat_tts_speed == 1
    assert config.dispatch_chat_tts_timeout_seconds == 30
    assert config.dispatch_chat_tts_max_input_chars == 1000
    assert config.dispatch_chat_tts_max_audio_bytes == 5_242_880
    assert config.dispatch_chat_queue_max_pending == 1000
    assert config.dispatch_chat_queue_workers == 4
    assert config.dispatch_chat_queue_media_workers == 1
    assert config.dispatch_chat_queue_max_attempts == 3
    assert config.dispatch_chat_queue_retry_initial_seconds == 2
    assert config.dispatch_chat_queue_retry_max_seconds == 30
    assert config.dispatch_chat_queue_job_ttl_seconds == 900
    assert config.dispatch_chat_queue_result_ttl_seconds == 1800
    assert config.dispatch_chat_timeout_seconds == 300


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dispatch_chat_queue_max_pending", 0),
        ("dispatch_chat_queue_max_pending", 100_001),
        ("dispatch_chat_queue_workers", 0),
        ("dispatch_chat_queue_workers", 65),
        ("dispatch_chat_queue_media_workers", 0),
        ("dispatch_chat_queue_media_workers", 17),
        ("dispatch_chat_memory_compression_queue_capacity", 0),
        ("dispatch_chat_memory_compression_queue_capacity", 1001),
        ("dispatch_chat_db_pool_size", 0),
        ("dispatch_chat_db_pool_size", 65),
        ("dispatch_chat_db_timeout_seconds", 0),
        ("dispatch_chat_db_timeout_seconds", 61),
        ("dispatch_chat_model_cache_ttl_seconds", -1),
        ("dispatch_chat_model_cache_ttl_seconds", 31),
        ("dispatch_chat_model_cache_ttl_seconds", float("inf")),
        ("dispatch_chat_queue_max_attempts", 0),
        ("dispatch_chat_queue_max_attempts", 11),
        ("dispatch_chat_queue_retry_initial_seconds", 0),
        ("dispatch_chat_queue_retry_max_seconds", 301),
        ("dispatch_chat_queue_job_ttl_seconds", 59),
        ("dispatch_chat_queue_result_ttl_seconds", 59),
    ],
)
def test_queue_rejects_unsafe_limits(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_config(**{field: value})


def test_queue_retry_maximum_cannot_be_less_than_initial_delay() -> None:
    with pytest.raises(ValidationError, match="retry"):
        make_config(
            dispatch_chat_queue_retry_initial_seconds=20,
            dispatch_chat_queue_retry_max_seconds=10,
        )


def test_queue_result_ttl_covers_the_full_job_lifetime() -> None:
    with pytest.raises(ValidationError, match="result TTL"):
        make_config(
            dispatch_chat_queue_job_ttl_seconds=600,
            dispatch_chat_queue_result_ttl_seconds=599,
        )


def test_media_generation_does_not_require_legacy_tts_url() -> None:
    enabled_without_legacy_endpoint = make_config(dispatch_chat_tts_enabled=True)
    assert enabled_without_legacy_endpoint.dispatch_chat_tts_url is None

    config = make_config(
        dispatch_chat_tts_enabled=True,
        dispatch_chat_tts_url="https://tts.example.invalid/v1/audio/speech",
        dispatch_chat_tts_token="tts-private-token-marker",
        dispatch_chat_tts_model="speech-model",
        dispatch_chat_tts_voice="friendly-voice",
        dispatch_chat_tts_response_format="opus",
        dispatch_chat_tts_speed=1.25,
    )

    assert config.dispatch_chat_tts_url == "https://tts.example.invalid/v1/audio/speech"
    assert config.dispatch_chat_tts_response_format == "opus"
    assert config.dispatch_chat_tts_speed == 1.25
    assert "tts-private-token-marker" not in repr(config)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dispatch_chat_image_timeout_seconds", 0),
        ("dispatch_chat_image_timeout_seconds", 121),
        ("dispatch_chat_image_max_bytes", 1023),
        ("dispatch_chat_image_max_bytes", 10_485_761),
        ("dispatch_chat_image_response_format", "gif"),
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
        make_config(dispatch_chat_local_workflow_base_url=url)


def test_enabled_local_workflow_requires_image_generation_and_mcp() -> None:
    with pytest.raises(ValidationError, match="image generation"):
        make_config(
            dispatch_chat_local_workflow_enabled=True,
            dispatch_chat_mcp_enabled=True,
        )
    with pytest.raises(ValidationError, match="MCP"):
        make_config(
            dispatch_chat_local_workflow_enabled=True,
            dispatch_chat_image_generation_enabled=True,
        )

    config = make_config(
        dispatch_chat_local_workflow_enabled=True,
        dispatch_chat_image_generation_enabled=True,
        dispatch_chat_mcp_enabled=True,
    )
    assert config.dispatch_chat_local_workflow_enabled is True


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
        make_config(dispatch_chat_tts_url=url)


def test_keyword_at_replies_are_trimmed_and_accept_safe_text() -> None:
    config = make_config(
        dispatch_chat_keyword_at_replies={
            "  在吗  ": "  我在。  ",
            "报时": "现在还不能报时。\n请稍后再试。",
        }
    )

    assert config.dispatch_chat_keyword_at_replies == {
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
        make_config(dispatch_chat_keyword_at_replies=rules)


def test_llm_request_options_accept_supported_boundaries() -> None:
    config = make_config(
        dispatch_chat_llm_max_tokens=128_000,
        dispatch_chat_llm_temperature=2,
        dispatch_chat_llm_top_p=1,
        dispatch_chat_llm_top_k=100,
        dispatch_chat_llm_min_p=0,
        dispatch_chat_llm_presence_penalty=-2,
        dispatch_chat_llm_frequency_penalty=2,
        dispatch_chat_llm_repetition_penalty=1,
        dispatch_chat_llm_stop=["END", "STOP"],
        dispatch_chat_llm_enable_thinking=True,
        dispatch_chat_llm_forward_user_name=True,
    )

    assert config.dispatch_chat_llm_max_tokens == 128_000
    assert config.dispatch_chat_llm_temperature == 2
    assert config.dispatch_chat_llm_top_p == 1
    assert config.dispatch_chat_llm_top_k == 100
    assert config.dispatch_chat_llm_min_p == 0
    assert config.dispatch_chat_llm_presence_penalty == -2
    assert config.dispatch_chat_llm_frequency_penalty == 2
    assert config.dispatch_chat_llm_repetition_penalty == 1
    assert config.dispatch_chat_llm_stop == ["END", "STOP"]
    assert config.dispatch_chat_llm_enable_thinking is True
    assert config.dispatch_chat_llm_forward_user_name is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dispatch_chat_llm_max_tokens", 0),
        ("dispatch_chat_llm_max_tokens", 128_001),
        ("dispatch_chat_llm_temperature", -0.01),
        ("dispatch_chat_llm_temperature", 2.01),
        ("dispatch_chat_llm_temperature", float("nan")),
        ("dispatch_chat_llm_top_p", -0.01),
        ("dispatch_chat_llm_top_p", 1.01),
        ("dispatch_chat_llm_top_k", 0),
        ("dispatch_chat_llm_top_k", 101),
        ("dispatch_chat_llm_min_p", -0.01),
        ("dispatch_chat_llm_min_p", 1.01),
        ("dispatch_chat_llm_presence_penalty", -2.01),
        ("dispatch_chat_llm_presence_penalty", 2.01),
        ("dispatch_chat_llm_frequency_penalty", -2.01),
        ("dispatch_chat_llm_frequency_penalty", 2.01),
        ("dispatch_chat_llm_repetition_penalty", 0.99),
        ("dispatch_chat_llm_repetition_penalty", 2.01),
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
        make_config(dispatch_chat_llm_stop=stop)


@pytest.mark.parametrize("field_name", [
    "dispatch_chat_agent_max_steps", "dispatch_chat_web_search_max_steps",
])
@pytest.mark.parametrize("max_steps", [0, 6])
def test_config_rejects_unbounded_agent_steps(field_name: str, max_steps: int) -> None:
    """普通聊天和旧联网配置遵守 Java Agent 相同的轮次上限。"""
    with pytest.raises(ValidationError):
        make_config(**{field_name: max_steps})


@pytest.mark.parametrize("max_steps", [None, 1, 5])
def test_chat_agent_accepts_optional_bounded_steps(max_steps: int | None) -> None:
    """可清空覆盖值，也可选择范围内的推理轮次。"""
    assert make_config(dispatch_chat_agent_max_steps=max_steps).dispatch_chat_agent_max_steps == (
        max_steps
    )


def test_general_mcp_services_accept_bounded_stable_codes() -> None:
    """普通 Agent 只接受可安全登记到 ai_mcp 的稳定服务 code。"""

    config = make_config(
        dispatch_chat_mcp_services=["windows-mcp", "internal_docs"],
    )

    assert config.dispatch_chat_mcp_services == ["windows-mcp", "internal_docs"]


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
    """配置不能绕过 Laya 专用开关，也不能扩大服务端三服务上限。"""

    with pytest.raises(ValidationError):
        make_config(dispatch_chat_mcp_services=services)


def test_laya_and_general_mcp_services_share_the_three_service_limit() -> None:
    """启用 Laya 后必须为它保留一个 MCP 服务名额。"""

    with pytest.raises(ValidationError, match="MCP"):
        make_config(
            dispatch_chat_mcp_enabled=True,
            dispatch_chat_laya_enabled=True,
            dispatch_chat_mcp_services=["windows-mcp", "docs", "files"],
        )


@pytest.mark.parametrize("max_images", [0, 9])
def test_image_recognition_rejects_unbounded_attachment_counts(max_images: int) -> None:
    with pytest.raises(ValidationError):
        make_config(dispatch_chat_max_images_per_message=max_images)


def test_config_repr_redacts_prompt_and_inbound_qq_allowlists() -> None:
    prompt = "private supplemental persona prompt"
    allowed_user = "940000000000010"
    allowed_group = "940000000000011"
    keyword_marker = "private-keyword-rule-marker"

    rendered = repr(
        make_config(
            dispatch_chat_system_prompt=prompt,
            dispatch_chat_allowed_users={allowed_user},
            dispatch_chat_allowed_groups={allowed_group},
            dispatch_chat_keyword_at_replies={keyword_marker: "private reply marker"},
        )
    )

    assert prompt not in rendered
    assert allowed_user not in rendered
    assert allowed_group not in rendered
    assert keyword_marker not in rendered


@pytest.mark.parametrize(
    "overrides",
    [
        {"dispatch_chat_auto_speak_groups": "20001"},
        {"dispatch_chat_auto_speak_users": "30001"},
    ],
)
def test_enabled_auto_speak_requires_bot_id(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="bot"):
        make_config(dispatch_chat_auto_speak_enabled=True, **overrides)


def test_enabled_auto_speak_requires_at_least_one_group() -> None:
    with pytest.raises(ValidationError, match="group"):
        make_config(
            dispatch_chat_auto_speak_enabled=True,
            dispatch_chat_auto_speak_bot_id="90001",
            dispatch_chat_auto_speak_users="30001",
            dispatch_chat_mcp_enabled=True,
            dispatch_chat_laya_enabled=True,
            dispatch_chat_group_recent_context_enabled=True,
        )


@pytest.mark.parametrize("mode", ["timer", "MESSAGE", ""])
def test_auto_speak_rejects_unknown_trigger_mode(mode: str) -> None:
    with pytest.raises(ValidationError):
        make_config(dispatch_chat_auto_speak_trigger_mode=mode)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"dispatch_chat_mcp_enabled": False}, "MCP"),
        ({"dispatch_chat_laya_enabled": False}, "Laya"),
        ({"dispatch_chat_group_recent_context_enabled": False}, "recent group context"),
    ],
)
def test_enabled_auto_speak_requires_decision_dependencies(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_enabled_auto_speak_config(**overrides)


def test_legacy_auto_speak_users_remain_readable_but_are_not_targets() -> None:
    config = make_enabled_auto_speak_config(
        dispatch_chat_auto_speak_users={str(value) for value in range(30001, 30022)}
    )

    assert len(config.dispatch_chat_auto_speak_users) == 21
    assert config.dispatch_chat_auto_speak_groups == {"20001"}


@pytest.mark.parametrize(
    ("field", "raw_value", "expected"),
    [
        ("dispatch_chat_auto_speak_groups", 20001, {"20001"}),
        ("dispatch_chat_auto_speak_groups", "20001", {"20001"}),
        (
            "dispatch_chat_auto_speak_groups",
            '["20001", "20002"]',
            {"20001", "20002"},
        ),
        (
            "dispatch_chat_auto_speak_groups",
            "20001, 20002,20001",
            {"20001", "20002"},
        ),
        (
            "dispatch_chat_auto_speak_groups",
            ["20001", 20002],
            {"20001", "20002"},
        ),
        ("dispatch_chat_auto_speak_users", 30001, {"30001"}),
        ("dispatch_chat_auto_speak_users", "30001", {"30001"}),
        (
            "dispatch_chat_auto_speak_users",
            '["30001", "30002"]',
            {"30001", "30002"},
        ),
        (
            "dispatch_chat_auto_speak_users",
            "30001, 30002,30001",
            {"30001", "30002"},
        ),
        (
            "dispatch_chat_auto_speak_users",
            ["30001", 30002],
            {"30001", "30002"},
        ),
    ],
)
def test_auto_speak_targets_accept_nonebot_environment_shapes(
    field: str,
    raw_value: object,
    expected: set[str],
) -> None:
    config = make_config(
        dispatch_chat_auto_speak_bot_id=90001,
        **{field: raw_value},
    )

    assert config.dispatch_chat_auto_speak_bot_id is not None
    assert config.dispatch_chat_auto_speak_bot_id.get_secret_value() == "90001"
    assert getattr(config, field) == expected


def test_auto_speak_targets_accept_real_nonebot_environment_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPATCH_CHAT_AUTO_SPEAK_ENABLED", "true")
    monkeypatch.setenv("DISPATCH_CHAT_AUTO_SPEAK_BOT_ID", "90001")
    monkeypatch.setenv("DISPATCH_CHAT_AUTO_SPEAK_GROUPS", '["20001", 20002]')
    monkeypatch.setenv("DISPATCH_CHAT_AUTO_SPEAK_USERS", "30001,30002")
    monkeypatch.setenv("DISPATCH_CHAT_MCP_ENABLED", "true")
    monkeypatch.setenv("DISPATCH_CHAT_LAYA_ENABLED", "true")
    monkeypatch.setenv("DISPATCH_CHAT_GROUP_RECENT_CONTEXT_ENABLED", "true")

    values = BaseSettings._settings_build_values(
        PluginConfig,
        {
            "onebot_v11_access_token": "test-onebot-access-token-with-enough-entropy",
            "dispatch_chat_session_secret": "test-session-secret-with-enough-entropy",
        },
        env_file=None,
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )
    config = PluginConfig.model_validate(values)

    assert config.dispatch_chat_auto_speak_groups == {"20001", "20002"}
    assert config.dispatch_chat_auto_speak_users == {"30001", "30002"}


def test_blank_auto_speak_values_disable_feature_through_nonebot_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DISPATCH_CHAT_AUTO_SPEAK_ENABLED", "false")
    monkeypatch.setenv("DISPATCH_CHAT_AUTO_SPEAK_BOT_ID", "")
    monkeypatch.setenv("DISPATCH_CHAT_AUTO_SPEAK_GROUPS", "")
    monkeypatch.setenv("DISPATCH_CHAT_AUTO_SPEAK_USERS", "")

    values = BaseSettings._settings_build_values(
        PluginConfig,
        {
            "onebot_v11_access_token": "test-onebot-access-token-with-enough-entropy",
            "dispatch_chat_session_secret": "test-session-secret-with-enough-entropy",
        },
        env_file=None,
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
    )
    config = PluginConfig.model_validate(values)

    assert config.dispatch_chat_auto_speak_bot_id is None
    assert config.dispatch_chat_auto_speak_groups == set()
    assert config.dispatch_chat_auto_speak_users == set()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dispatch_chat_auto_speak_bot_id", "1234"),
        ("dispatch_chat_auto_speak_bot_id", "01234"),
        ("dispatch_chat_auto_speak_bot_id", "123456789012345678901"),
        ("dispatch_chat_auto_speak_bot_id", "\uff11\uff12\uff13\uff14\uff15"),
        ("dispatch_chat_auto_speak_groups", "1234"),
        ("dispatch_chat_auto_speak_groups", "01234"),
        ("dispatch_chat_auto_speak_groups", "123456789012345678901"),
        ("dispatch_chat_auto_speak_groups", "group-20001"),
        ("dispatch_chat_auto_speak_users", "1234"),
        ("dispatch_chat_auto_speak_users", "01234"),
        ("dispatch_chat_auto_speak_users", "123456789012345678901"),
        ("dispatch_chat_auto_speak_users", "user-30001"),
    ],
)
def test_auto_speak_rejects_invalid_qq_identifiers(field: str, value: object) -> None:
    base: dict[str, object] = {
        "dispatch_chat_auto_speak_enabled": True,
        "dispatch_chat_auto_speak_bot_id": "90001",
        "dispatch_chat_auto_speak_groups": "20001",
    }
    base[field] = value

    with pytest.raises(ValidationError):
        make_config(**base)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dispatch_chat_auto_speak_initial_delay_seconds", 9.99),
        ("dispatch_chat_auto_speak_interval_seconds", 59.99),
        ("dispatch_chat_auto_speak_prompt", ""),
        ("dispatch_chat_auto_speak_prompt", "x" * 1001),
        ("dispatch_chat_auto_speak_laya_prompt", ""),
        ("dispatch_chat_auto_speak_laya_prompt", "x" * 1001),
        ("dispatch_chat_auto_speak_max_reply_chars", 0),
        ("dispatch_chat_auto_speak_max_reply_chars", 1001),
    ],
)
def test_auto_speak_rejects_invalid_bounds(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_config(**{field: value})


def test_auto_speak_accepts_identifier_and_length_boundaries() -> None:
    config = make_enabled_auto_speak_config(
        dispatch_chat_auto_speak_bot_id="12345",
        dispatch_chat_auto_speak_groups=["23456", "12345678901234567890"],
        dispatch_chat_auto_speak_prompt="x" * 1000,
        dispatch_chat_auto_speak_laya_prompt="y" * 1000,
        dispatch_chat_auto_speak_initial_delay_seconds=10,
        dispatch_chat_auto_speak_interval_seconds=60,
        dispatch_chat_auto_speak_max_reply_chars=1000,
    )

    assert config.dispatch_chat_auto_speak_groups == {
        "23456",
        "12345678901234567890",
    }


def test_message_trigger_auto_speak_accepts_unused_sub_minute_interval() -> None:
    config = make_enabled_auto_speak_config(
        dispatch_chat_auto_speak_trigger_mode="message",
        dispatch_chat_auto_speak_interval_seconds=1,
    )

    assert config.dispatch_chat_auto_speak_interval_seconds == 1


def test_auto_speak_rejects_more_than_twenty_targets() -> None:
    with pytest.raises(ValidationError, match=r"20|target"):
        make_config(
            dispatch_chat_auto_speak_enabled=True,
            dispatch_chat_auto_speak_bot_id="90001",
            dispatch_chat_auto_speak_groups={str(value) for value in range(10000, 10021)},
        )


def test_auto_speak_reserves_one_ai_concurrency_slot_for_interactive_chat() -> None:
    with pytest.raises(ValidationError, match=r"concurrent|interactive"):
        make_enabled_auto_speak_config(
            dispatch_chat_max_concurrent_requests=1,
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
        dispatch_chat_auto_speak_bot_id=sensitive_values["bot"],
        dispatch_chat_auto_speak_groups=sensitive_values["group"],
        dispatch_chat_auto_speak_users=sensitive_values["user"],
        dispatch_chat_auto_speak_prompt=sensitive_values["prompt"],
        dispatch_chat_auto_speak_laya_prompt=sensitive_values["laya_prompt"],
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
            dispatch_chat_auto_speak_enabled=True,
            dispatch_chat_auto_speak_bot_id=sensitive_values["bot"],
            dispatch_chat_auto_speak_groups=sensitive_values["group"],
            dispatch_chat_auto_speak_users=sensitive_values["user"],
            dispatch_chat_auto_speak_prompt=sensitive_values["prompt"],
            dispatch_chat_auto_speak_laya_prompt=sensitive_values["laya_prompt"],
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
            dispatch_chat_session_secret="test-session-secret-with-enough-entropy",
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://private-user@example.invalid/dispatch-ai",
        "https://private-user:private-password@example.invalid/dispatch-ai",
    ],
)
def test_config_rejects_ai_url_userinfo_without_disclosing_it(url: str) -> None:
    with pytest.raises(ValidationError) as exc_info:
        make_config(dispatch_chat_ai_base_url=url)

    assert "private-user" not in str(exc_info.value)
    assert "private-password" not in str(exc_info.value)


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
            dispatch_chat_session_secret="test-session-secret-with-enough-entropy",
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
        dispatch_chat_session_secret="test-session-secret-with-enough-entropy",
        dispatch_chat_history_turns=1000,
        dispatch_chat_group_recent_context_messages=500,
    )

    assert config.dispatch_chat_history_turns == 1000
    assert config.dispatch_chat_group_recent_context_messages == 500


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dispatch_chat_ai_base_url", "file:///tmp/secret"),
        ("dispatch_chat_timeout_seconds", 0),
        ("dispatch_chat_max_input_chars", 0),
        ("dispatch_chat_history_turns", -1),
        ("dispatch_chat_group_recent_context_messages", 0),
        ("dispatch_chat_group_recent_context_max_chars", 255),
        ("dispatch_chat_group_recent_context_max_chars", 12_001),
        ("dispatch_chat_group_recent_context_ttl_seconds", 9),
        ("dispatch_chat_memory_max_chars", 255),
        ("dispatch_chat_memory_max_chars", 8001),
        ("dispatch_chat_memory_compression_max_tokens", 63),
        ("dispatch_chat_memory_compression_max_tokens", 4097),
        ("dispatch_chat_history_ttl_seconds", 0),
        ("dispatch_chat_max_sessions", 0),
        ("dispatch_chat_max_concurrent_requests", 0),
        ("dispatch_chat_chunk_chars", 0),
        ("dispatch_chat_max_reply_chars", 0),
        ("dispatch_chat_cooldown_seconds", -0.1),
        ("dispatch_chat_naraka_timeout_seconds", 0),
        ("dispatch_chat_naraka_cooldown_seconds", -0.1),
        ("dispatch_chat_naraka_max_concurrent_requests", 0),
        ("dispatch_chat_config_refresh_interval_seconds", 0.5),
        ("dispatch_chat_config_refresh_interval_seconds", 301),
        ("dispatch_chat_system_prompt", ""),
        ("dispatch_chat_system_prompt", "x" * 20_001),
        ("dispatch_chat_vision_model", ""),
        ("dispatch_chat_vision_model", "x" * 256),
        ("dispatch_chat_vision_model", "vision\0model"),
        ("dispatch_chat_image_model", ""),
        ("dispatch_chat_image_model", "x" * 256),
        ("dispatch_chat_image_model", "image\0model"),
    ],
)
def test_config_rejects_invalid_bounds(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_config(**{field: value})


def test_config_rejects_chunk_larger_than_total_reply() -> None:
    with pytest.raises(ValidationError, match="chunk"):
        make_config(
            dispatch_chat_chunk_chars=200,
            dispatch_chat_max_reply_chars=100,
        )


def test_config_rejects_reused_secrets_and_unsafe_priorities() -> None:
    shared_secret = "shared-secret-that-is-long-enough-for-validation"
    with pytest.raises(ValidationError, match="must be different"):
        PluginConfig(
            onebot_v11_access_token=shared_secret,
            dispatch_chat_session_secret=shared_secret,
        )

    with pytest.raises(ValidationError, match="reset priority"):
        make_config(
            dispatch_chat_priority=50,
            dispatch_chat_reset_priority=50,
        )


def test_config_token_is_hidden_from_repr() -> None:
    config = make_config(
        dispatch_chat_ai_token="top-secret",
        dispatch_chat_tts_token="tts-top-secret",
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
        make_config(dispatch_chat_naraka_cookie="pkey=secret")

    with pytest.raises(ValidationError, match="Naraka credentials"):
        make_config(dispatch_chat_naraka_heybox_id="123456")


def test_blank_optional_naraka_settings_disable_feature() -> None:
    config = make_config(
        dispatch_chat_naraka_cookie="",
        dispatch_chat_naraka_heybox_id="",
        dispatch_chat_naraka_default_role_id="",
    )

    assert config.dispatch_chat_naraka_cookie is None
    assert config.dispatch_chat_naraka_heybox_id is None
    assert config.dispatch_chat_naraka_default_role_id is None


def test_numeric_naraka_query_settings_are_normalized_to_strings() -> None:
    config = make_config(
        dispatch_chat_naraka_server=163,
        dispatch_chat_naraka_battle_tid=5000001,
    )

    assert config.dispatch_chat_naraka_server == "163"
    assert config.dispatch_chat_naraka_battle_tid == "5000001"


def test_numeric_naraka_account_and_role_ids_are_normalized_to_secrets() -> None:
    config = make_config(
        dispatch_chat_naraka_cookie=(
            "pkey=fake; x_heybox_id=123456; x_pkey=fake; x_xhh_tokenid=fake-token"
        ),
        dispatch_chat_naraka_heybox_id=123456,
        dispatch_chat_naraka_default_role_id=123456,
    )

    assert config.dispatch_chat_naraka_heybox_id is not None
    assert config.dispatch_chat_naraka_heybox_id.get_secret_value() == "123456"
    assert config.dispatch_chat_naraka_default_role_id is not None
    assert config.dispatch_chat_naraka_default_role_id.get_secret_value() == "123456"


def test_naraka_credentials_and_default_role_are_hidden() -> None:
    config = make_config(
        dispatch_chat_naraka_cookie=(
            "pkey=fake; x_heybox_id=123456; x_pkey=fake; x_xhh_tokenid=fake-token"
        ),
        dispatch_chat_naraka_heybox_id="123456",
        dispatch_chat_naraka_default_role_id="private_role_123",
    )

    rendered = repr(config)
    assert "fake-token" not in rendered
    assert "123456" not in rendered
    assert "private_role_123" not in rendered


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dispatch_chat_naraka_heybox_id", "not-a-number"),
        ("dispatch_chat_naraka_default_role_id", "https://attacker.invalid"),
        ("dispatch_chat_naraka_server", "../server"),
        ("dispatch_chat_naraka_season", "bad season"),
        ("dispatch_chat_naraka_battle_tid", ""),
    ],
)
def test_config_rejects_invalid_naraka_identifiers(field: str, value: str) -> None:
    base: dict[str, object] = {
        "dispatch_chat_naraka_cookie": (
            "pkey=fake; x_heybox_id=123456; x_pkey=fake; x_xhh_tokenid=fake-token"
        ),
        "dispatch_chat_naraka_heybox_id": "123456",
    }
    base[field] = value

    with pytest.raises(ValidationError):
        make_config(**base)
