from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent_bot.config import RuntimeConfig
from agent_bot.content_classifier import ContentCategory
from agent_bot.memory_store import (
    MySqlConversationMemoryStore,
    MySqlRecentGroupContextStore,
)
from agent_bot.models import ChatMessage
from agent_bot.runtime_reload import SettingsReloader
from agent_bot.settings import (
    SettingsValidationError,
    VersionedSettings,
    runtime_config_to_plain_values,
)
from agent_bot.state import AccessPolicy


@pytest.mark.asyncio
async def test_failed_runtime_publish_restores_previous_auto_speak(chat_plugin, monkeypatch):
    """发布业务快照失败时清理替换资源并恢复旧自动发言，不依赖 Token 热重连。"""
    original = chat_plugin._runtime_state
    monkeypatch.setattr(
        chat_plugin, "_activate_runtime_state", Mock(side_effect=RuntimeError("publish failed")),
    )
    monkeypatch.setattr(chat_plugin, "stop_auto_speak", AsyncMock())
    monkeypatch.setattr(chat_plugin, "start_auto_speak", AsyncMock())
    monkeypatch.setattr(chat_plugin, "_close_runtime_state", AsyncMock())
    monkeypatch.setattr(
        chat_plugin, "_build_runtime_state",
        lambda config, previous: replace(original, config=config),
    )
    values = runtime_config_to_plain_values(original.config)
    values["chat_system_prompt"] = "a changed runtime prompt"
    changed = RuntimeConfig.model_validate(values)
    try:
        with pytest.raises(RuntimeError, match="publish failed"):
            await chat_plugin._apply_runtime_settings(VersionedSettings(config=changed, revision=2))
        assert chat_plugin._runtime_state is original
        # 新快照未发布时，旧自动发言任务仍属于当前运行时，必须恢复它的生命周期。
        chat_plugin.start_auto_speak.assert_awaited_once()
        chat_plugin._close_runtime_state.assert_awaited_once()
    finally:
        chat_plugin._runtime_state = original
        chat_plugin._publish_runtime_state(original)


def runtime_config(*, model: str = "model-v1") -> RuntimeConfig:
    return RuntimeConfig(
        onebot_v11_access_token="runtime-reload-onebot-token-with-enough-entropy",
        chat_session_secret="runtime-reload-session-secret-with-enough-entropy",
        chat_model=model,
    )


@pytest.mark.parametrize("revision", [0, -1, True])
def test_reloader_rejects_invalid_initial_revision(revision: int) -> None:
    with pytest.raises(ValueError, match="positive"):
        SettingsReloader(Mock(), initial_revision=revision, apply=AsyncMock())


@pytest.mark.asyncio
async def test_unchanged_revision_avoids_loading_full_snapshot() -> None:
    store = Mock()
    store.load_revision.return_value = 7
    apply = AsyncMock()
    reloader = SettingsReloader(store, initial_revision=7, apply=apply)

    changed = await reloader.refresh_once()

    assert changed is False
    store.load_revision.assert_called_once_with()
    store.load.assert_not_called()
    apply.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_revision_loads_and_applies_validated_snapshot() -> None:
    updated = VersionedSettings(config=runtime_config(model="model-v2"), revision=8)
    store = Mock()
    store.load_revision.return_value = 8
    store.load.return_value = updated
    apply = AsyncMock()
    reloader = SettingsReloader(store, initial_revision=7, apply=apply)

    changed = await reloader.refresh_once()

    assert changed is True
    apply.assert_awaited_once_with(updated)
    assert reloader.revision == 8


@pytest.mark.asyncio
async def test_failed_apply_keeps_previous_revision_for_retry() -> None:
    updated = VersionedSettings(config=runtime_config(model="model-v2"), revision=8)
    store = Mock()
    store.load_revision.return_value = 8
    store.load.return_value = updated
    apply = AsyncMock(side_effect=RuntimeError("apply failed"))
    reloader = SettingsReloader(store, initial_revision=7, apply=apply)

    with pytest.raises(RuntimeError, match="apply failed"):
        await reloader.refresh_once()

    assert reloader.revision == 7


@pytest.mark.asyncio
async def test_refresh_failure_details_redact_untrusted_runtime_error_message() -> None:
    """运行时构建异常只暴露阶段和类型，不得把异常正文原样写入日志。"""

    updated = VersionedSettings(config=runtime_config(model="model-v2"), revision=8)
    store = Mock()
    store.load_revision.return_value = 8
    store.load.return_value = updated
    sensitive_detail = "token=plaintext-must-not-appear"
    reloader = SettingsReloader(
        store,
        initial_revision=7,
        apply=AsyncMock(side_effect=RuntimeError(sensitive_detail)),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await reloader.refresh_once()

    assert reloader.failure_details(exc_info.value) == (
        "runtime_apply",
        "RuntimeError",
        "runtime settings apply failed",
    )
    assert sensitive_detail not in str(reloader.failure_details(exc_info.value))


@pytest.mark.asyncio
async def test_refresh_failure_reports_mysql_code_without_driver_message() -> None:
    """数据库故障保留可定位的错误码，但不暴露驱动消息中的连接凭据。"""
    from pymysql.err import OperationalError

    from agent_bot.settings import DatabaseParams, SettingsError, SettingsStore

    store = SettingsStore(
        DatabaseParams("localhost", 3306, "test", "test", "test"),
        connect=Mock(side_effect=OperationalError(2013, "private-connection-details")),
    )
    reloader = SettingsReloader(store, initial_revision=7, apply=AsyncMock())
    with pytest.raises(SettingsError) as caught:
        await reloader.refresh_once()

    stage, error_type, reason = reloader.failure_details(caught.value)
    assert (stage, error_type) == ("revision_check", "SettingsError")
    assert "OperationalError" in reason
    assert "2013" in reason
    assert "private-connection-details" not in reason
    assert reloader.revision == 7


@pytest.mark.asyncio
async def test_snapshot_race_uses_revision_returned_by_full_load() -> None:
    updated = VersionedSettings(config=runtime_config(model="model-v3"), revision=9)
    store = Mock()
    store.load_revision.return_value = 8
    store.load.return_value = updated
    apply = AsyncMock()
    reloader = SettingsReloader(store, initial_revision=7, apply=apply)

    assert await reloader.refresh_once() is True
    assert reloader.revision == 9
    apply.assert_awaited_once_with(updated)


@pytest.mark.asyncio
async def test_refresh_loop_logs_safe_stage_type_and_validation_reason(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """自动刷新失败日志应定位快照加载字段，同时不记录配置原值。"""

    from agent_bot.core import lifecycle

    store = Mock()
    store.load_revision.return_value = 8
    store.load.side_effect = SettingsValidationError(
        "settings snapshot failed validation: chat_tts_token[string_type]"
    )
    reloader = SettingsReloader(store, initial_revision=7, apply=AsyncMock())
    logger = Mock()
    sleeps = 0

    async def stop_after_first_attempt(_seconds: float) -> None:
        """允许一次刷新尝试，然后终止无限轮询。"""

        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(lifecycle, "logger", logger)
    monkeypatch.setattr(lifecycle.asyncio, "sleep", stop_after_first_attempt)

    with pytest.raises(asyncio.CancelledError):
        await lifecycle.settings_refresh_loop(lambda: runtime_config(), reloader)

    logger.warning.assert_called_once_with(
        "配置自动刷新失败，继续使用上一版本：阶段={} 异常={} 原因={}",
        "snapshot_load",
        "SettingsValidationError",
        "settings snapshot failed validation: chat_tts_token[string_type]",
    )


def test_effective_runtime_config_hot_reloads_chat_but_keeps_startup_fields(
    chat_plugin,
) -> None:
    current = chat_plugin.plugin_config
    values = runtime_config_to_plain_values(current)
    values.update(
        {
            "chat_model": "hot-model-v2",
            "chat_vision_model": "hot-vision-model-v2",
            "chat_image_model": "hot-image-model-v2",
            "chat_system_prompt": "数据库热更新后的唯一系统提示词",
            "chat_auto_speak_prompt": "热更新后的主动回复指令",
            "chat_auto_speak_laya_prompt": "热更新后的 Laya 判断指令",
            "chat_allowed_users": ["940000000000012"],
            "chat_llm_temperature": 0.4,
            "chat_llm_enable_thinking": True,
            "chat_llm_forward_user_name": True,
            "chat_agent_max_steps": 2,
            "chat_mcp_services": ["windows-mcp"],
            "chat_web_search_enabled": True,
            "chat_web_search_max_steps": 4,
            "chat_image_recognition_enabled": False,
            "chat_max_images_per_message": 7,
            "chat_memory_enabled": False,
            "chat_memory_max_chars": 2048,
            "chat_memory_compression_max_tokens": 256,
            "chat_keyword_at_replies": {"热更新关键词": "热更新回复"},
            "chat_auto_mention_enabled": False,
            "chat_mention_tool_enabled": False,
            "chat_tts_enabled": True,
            "chat_tts_url": "https://tts.example.invalid/v1/audio/speech",
            "chat_tts_model": "hot-speech-model",
            "chat_tts_voice": "hot-speech-voice",
            "onebot_v11_access_token": ("replacement-onebot-token-that-still-requires-a-restart"),
            "chat_priority": current.chat_priority + 10,
        }
    )
    candidate = RuntimeConfig.model_validate(values)

    effective, restart_fields = chat_plugin._effective_runtime_config(candidate, current)

    assert effective.chat_model == "hot-model-v2"
    assert effective.chat_vision_model == "hot-vision-model-v2"
    assert effective.chat_image_model == "hot-image-model-v2"
    assert effective.chat_system_prompt == "数据库热更新后的唯一系统提示词"
    assert effective.chat_auto_speak_prompt == "热更新后的主动回复指令"
    assert effective.chat_auto_speak_laya_prompt == "热更新后的 Laya 判断指令"
    assert effective.chat_allowed_users == {"940000000000012"}
    assert effective.chat_llm_temperature == 0.4
    assert effective.chat_llm_enable_thinking is True
    assert effective.chat_llm_forward_user_name is True
    assert effective.chat_agent_max_steps == 2
    assert effective.chat_mcp_services == ["windows-mcp"]
    assert effective.chat_web_search_enabled is True
    assert effective.chat_web_search_max_steps == 4
    assert effective.chat_image_recognition_enabled is False
    assert effective.chat_max_images_per_message == 7
    assert effective.chat_memory_enabled is False
    assert effective.chat_memory_max_chars == 2048
    assert effective.chat_memory_compression_max_tokens == 256
    assert effective.chat_keyword_at_replies == {"热更新关键词": "热更新回复"}
    assert effective.chat_auto_mention_enabled is False
    assert effective.chat_mention_tool_enabled is False
    assert effective.chat_tts_enabled is True
    assert effective.chat_tts_url == "https://tts.example.invalid/v1/audio/speech"
    assert effective.chat_tts_model == "hot-speech-model"
    assert effective.chat_tts_voice == "hot-speech-voice"
    assert effective.onebot_v11_access_token == current.onebot_v11_access_token
    assert effective.chat_priority == current.chat_priority
    assert restart_fields == {"onebot_v11_access_token", "chat_priority"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_steps", "enable_thinking"), [(1, False), (5, True), (None, None)],
)
async def test_chat_reasoning_settings_hot_reload_into_outgoing_request(
    chat_plugin, max_steps: int | None, enable_thinking: bool | None,
) -> None:
    """热更新或清空轮次和思考开关后，下一次普通聊天使用新配置。"""
    current = chat_plugin._runtime_state
    values = runtime_config_to_plain_values(current.config)
    values.update({
        "chat_agent_max_steps": 2,
        "chat_web_search_max_steps": 4,
        "chat_llm_enable_thinking": not enable_thinking,
    })
    before = RuntimeConfig.model_validate(values)
    values.update({
        "chat_agent_max_steps": max_steps,
        "chat_llm_enable_thinking": enable_thinking,
    })
    candidate = RuntimeConfig.model_validate(values)
    effective, restart_fields = chat_plugin._effective_runtime_config(candidate, before)
    assert restart_fields == set()
    replacement = chat_plugin._build_runtime_state(effective, current)

    # 配置传播验证只替换模型调用，实际 Agent 请求仍由本地客户端构造。
    complete = AsyncMock(return_value=SimpleNamespace(content="你好"))
    replacement.ai_client.agent_service.complete = complete
    try:
        reply = await replacement.ai_client.complete(
            model="model", session_id="session", prompt="你好", request_id="reload-reasoning",
            messages=[ChatMessage(role="user", content="你好")], chat_tools=("memory",),
            tool_session_token="a" * 40, use_web_search=False,
        )
        assert reply == "你好"
        request = complete.await_args.args[0]
        assert request.max_steps == max_steps
        assert request.options.enable_thinking == enable_thinking
    finally:
        await chat_plugin._close_runtime_state(replacement)


@pytest.mark.asyncio
async def test_runtime_state_uses_native_audio_without_legacy_tts_client(chat_plugin) -> None:
    current = chat_plugin._runtime_state
    values = runtime_config_to_plain_values(current.config)
    values.update(
        {
            "chat_tts_enabled": True,
            "chat_tts_url": None,
        }
    )
    candidate = RuntimeConfig.model_validate(values)

    replacement = chat_plugin._build_runtime_state(candidate, current)
    ai_client = replacement.ai_client
    assert replacement.tts_client is None
    assert ai_client.python_service.providers["openai-chat"].transport.http.is_closed is False

    await chat_plugin._close_runtime_state(replacement)

    assert ai_client.python_service.providers["openai-chat"].transport.http.is_closed is True


@pytest.mark.asyncio
async def test_runtime_state_builds_gpt_sovits_client_from_database_settings(
    chat_plugin,
    tmp_path,
) -> None:
    """GPT-SoVITS 参数未从数据库快照构建或旧客户端未关闭时应失败。"""

    current = chat_plugin._runtime_state
    values = runtime_config_to_plain_values(current.config)
    values.update(
        {
            "chat_tts_enabled": True,
            "chat_tts_url": "http://127.0.0.1:9880/tts",
            "chat_tts_reference_audio_path": "G:/models/hutao/reference.wav",
            "chat_tts_reference_text": "胡桃参考文本",
            "chat_tts_reference_language": "zh",
            "chat_tts_text_language": "zh",
            "chat_tts_output_directory": str(tmp_path),
            "chat_tts_speed": 1.1,
            "chat_tts_timeout_seconds": 45,
            "chat_tts_max_input_chars": 512,
            "chat_tts_max_audio_bytes": 2_000_000,
        }
    )
    candidate = RuntimeConfig.model_validate(values)

    replacement = chat_plugin._build_runtime_state(candidate, current)
    assert replacement.tts_client is not None
    assert replacement.tts_client._http.is_closed is False

    await chat_plugin._close_runtime_state(replacement)

    assert replacement.tts_client._http.is_closed is True


@pytest.mark.asyncio
async def test_runtime_state_routes_each_content_category_to_hot_reloaded_model(
    chat_plugin,
) -> None:
    current = chat_plugin._runtime_state
    values = runtime_config_to_plain_values(current.config)
    values.update(
        {
            "chat_model": "hot-text-model",
            "chat_vision_model": "hot-vision-model",
            "chat_image_model": "hot-image-model",
            "chat_tts_model": "hot-audio-model",
        }
    )
    candidate = RuntimeConfig.model_validate(values)

    replacement = chat_plugin._build_runtime_state(candidate, current)
    try:
        router = replacement.chat_service.model_router
        assert router.model_for(ContentCategory.TEXT) == "hot-text-model"
        assert router.model_for(ContentCategory.IMAGE_UNDERSTANDING) == "hot-vision-model"
        assert router.model_for(ContentCategory.IMAGE_GENERATION) == "hot-image-model"
        assert router.model_for(ContentCategory.AUDIO_GENERATION) == "hot-audio-model"
    finally:
        await chat_plugin._close_runtime_state(replacement)


@pytest.mark.asyncio
async def test_runtime_state_uses_only_database_configured_system_prompt(
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = chat_plugin._runtime_state
    values = runtime_config_to_plain_values(current.config)
    values["chat_system_prompt"] = "数据库中的唯一提示词"
    values["chat_agent_enabled"] = False
    candidate = RuntimeConfig.model_validate(values)
    service_factory = Mock(wraps=chat_plugin.ChatService)
    monkeypatch.setattr(chat_plugin, "ChatService", service_factory)

    replacement = chat_plugin._build_runtime_state(candidate, current)
    try:
        service_options = service_factory.call_args.kwargs
        assert service_options["system_prompt"] == "数据库中的唯一提示词"
        # 数据库中的旧开关不再进入运行装配，不能把自然聊天切回普通模型。
        assert "agent_enabled" not in service_options
    finally:
        await chat_plugin._close_runtime_state(replacement)


@pytest.mark.asyncio
async def test_runtime_state_hot_reloads_memory_limits_safely(
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = chat_plugin._runtime_state
    token_values = runtime_config_to_plain_values(current.config)
    token_values["chat_memory_compression_max_tokens"] = 256
    token_candidate = RuntimeConfig.model_validate(token_values)
    service_factory = Mock(wraps=chat_plugin.ChatService)
    monkeypatch.setattr(chat_plugin, "ChatService", service_factory)
    token_replacement = chat_plugin._build_runtime_state(token_candidate, current)
    token_service_options = service_factory.call_args.kwargs

    limit_values = runtime_config_to_plain_values(current.config)
    limit_values["chat_memory_max_chars"] = 2048
    limit_candidate = RuntimeConfig.model_validate(limit_values)
    limit_replacement = chat_plugin._build_runtime_state(limit_candidate, current)
    try:
        assert token_replacement.conversation_store is current.conversation_store
        assert token_service_options["memory_compression_max_tokens"] == 256
        assert limit_replacement.conversation_store is current.conversation_store
        assert isinstance(limit_replacement.conversation_store, MySqlConversationMemoryStore)
        assert (
            limit_replacement.conversation_store.policy.max_memory_chars
            == current.config.chat_memory_max_chars
        )
        chat_plugin._publish_runtime_state(limit_replacement)
        assert limit_replacement.conversation_store.policy.max_memory_chars == 2048
    finally:
        chat_plugin._publish_runtime_state(current)
        await chat_plugin._close_runtime_state(token_replacement)
        await chat_plugin._close_runtime_state(limit_replacement)


@pytest.mark.asyncio
async def test_runtime_state_rebuilds_recent_group_context_only_when_its_limits_change(
    chat_plugin,
) -> None:
    current = chat_plugin._runtime_state
    same_values = runtime_config_to_plain_values(current.config)
    same_values["chat_llm_temperature"] = 0.3
    same_candidate = RuntimeConfig.model_validate(same_values)
    same_replacement = chat_plugin._build_runtime_state(same_candidate, current)

    changed_values = runtime_config_to_plain_values(current.config)
    changed_values["chat_group_recent_context_messages"] = 5
    changed_candidate = RuntimeConfig.model_validate(changed_values)
    changed_replacement = chat_plugin._build_runtime_state(changed_candidate, current)
    try:
        assert (
            same_replacement.group_recent_context_store
            is current.group_recent_context_store
        )
        assert changed_replacement.group_recent_context_store is current.group_recent_context_store
        assert isinstance(
            changed_replacement.group_recent_context_store,
            MySqlRecentGroupContextStore,
        )
        assert (
            changed_replacement.group_recent_context_store.policy.message_limit
            == current.config.chat_group_recent_context_messages
        )
        chat_plugin._publish_runtime_state(changed_replacement)
        assert changed_replacement.group_recent_context_store.policy.message_limit == 5
    finally:
        chat_plugin._publish_runtime_state(current)
        await chat_plugin._close_runtime_state(same_replacement)
        await chat_plugin._close_runtime_state(changed_replacement)


@pytest.mark.asyncio
async def test_runtime_state_builds_group_only_laya_auto_speak(chat_plugin) -> None:
    current = chat_plugin._runtime_state
    values = runtime_config_to_plain_values(current.config)
    values.update(
        {
            "chat_auto_speak_enabled": True,
            "chat_auto_speak_bot_id": "940000000000001",
            "chat_auto_speak_groups": ["940000000000002"],
            "chat_auto_speak_trigger_mode": "message",
            "chat_mcp_enabled": True,
            "chat_auto_speak_mcp_code": "decision",
            "chat_group_recent_context_enabled": True,
        }
    )
    candidate = RuntimeConfig.model_validate(values)

    replacement = chat_plugin._build_runtime_state(candidate, current)
    try:
        service = replacement.auto_speak_service
        assert service is not None
        assert service.runs_on_interval is False
        assert service.accepts_message("940000000000001", "940000000000002") is True
        assert service.accepts_message("940000000000001", "940000000000003") is False
        assert service._decision_client.catalog is replacement.ai_client.agent_service.catalog
        assert service._recent_context_store is replacement.group_recent_context_store
        assert not hasattr(service, "_targets")
    finally:
        await chat_plugin._close_runtime_state(replacement)


@pytest.mark.asyncio
async def test_memory_schema_initialization_runs_outside_event_loop(
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event_loop_thread = threading.get_ident()
    call_threads: list[int] = []

    def initialize() -> None:
        call_threads.append(threading.get_ident())

    monkeypatch.setattr(chat_plugin._memory_schema, "initialize", initialize)

    await chat_plugin.initialize_chat_memory()

    assert call_threads
    assert all(thread_id != event_loop_thread for thread_id in call_threads)


@pytest.mark.asyncio
async def test_apply_runtime_settings_atomically_publishes_new_chat_state(
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_state = chat_plugin._runtime_state
    values = runtime_config_to_plain_values(original_state.config)
    values.update(
        {
            "chat_model": "hot-model-v3",
            "chat_allowed_users": ["940000000000013"],
        }
    )
    candidate = RuntimeConfig.model_validate(values)
    replacement_state = replace(
        original_state,
        config=candidate,
        access_policy=AccessPolicy(allowed_users={"940000000000013"}),
    )
    stop_auto = AsyncMock()
    start_auto = AsyncMock()
    close_old = AsyncMock()
    monkeypatch.setattr(chat_plugin, "_build_runtime_state", Mock(return_value=replacement_state))
    monkeypatch.setattr(chat_plugin, "stop_auto_speak", stop_auto)
    monkeypatch.setattr(chat_plugin, "start_auto_speak", start_auto)
    monkeypatch.setattr(chat_plugin, "_close_runtime_state", close_old)

    try:
        await chat_plugin._apply_runtime_settings(VersionedSettings(config=candidate, revision=2))

        assert chat_plugin.plugin_config.chat_model == "hot-model-v3"
        assert chat_plugin.access_policy.allowed_users == frozenset({"940000000000013"})
        stop_auto.assert_awaited_once_with()
        start_auto.assert_awaited_once_with()
        close_old.assert_awaited_once_with(original_state)
    finally:
        chat_plugin._runtime_state = original_state
        chat_plugin._publish_runtime_state(original_state)


def test_runtime_state_repr_does_not_expose_session_secret(chat_plugin) -> None:
    assert chat_plugin._runtime_state.session_secret not in repr(chat_plugin._runtime_state)


@pytest.mark.asyncio
async def test_apply_skips_runtime_rebuild_when_only_startup_fields_changed(
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_state = chat_plugin._runtime_state
    values = runtime_config_to_plain_values(original_state.config)
    values["chat_priority"] = original_state.config.chat_priority + 1
    candidate = RuntimeConfig.model_validate(values)
    build = Mock()
    stop_auto = AsyncMock()
    monkeypatch.setattr(chat_plugin, "_build_runtime_state", build)
    monkeypatch.setattr(chat_plugin, "stop_auto_speak", stop_auto)

    try:
        await chat_plugin._apply_runtime_settings(
            VersionedSettings(config=candidate, revision=2)
        )

        assert chat_plugin._runtime_state is original_state
        build.assert_not_called()
        stop_auto.assert_not_awaited()
    finally:
        chat_plugin._runtime_state = original_state
        chat_plugin._publish_runtime_state(original_state)


@pytest.mark.asyncio
async def test_apply_runtime_settings_waits_for_in_flight_message(
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_state = chat_plugin._runtime_state
    values = runtime_config_to_plain_values(original_state.config)
    values["chat_model"] = "hot-model-after-in-flight-message"
    candidate = RuntimeConfig.model_validate(values)
    replacement_state = replace(original_state, config=candidate)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def hold_runtime_lease() -> None:
        async with chat_plugin._runtime_lease():
            entered.set()
            await release.wait()

    monkeypatch.setattr(chat_plugin, "_build_runtime_state", Mock(return_value=replacement_state))
    monkeypatch.setattr(chat_plugin, "stop_auto_speak", AsyncMock())
    monkeypatch.setattr(chat_plugin, "start_auto_speak", AsyncMock())
    monkeypatch.setattr(chat_plugin, "_close_runtime_state", AsyncMock())

    holder = asyncio.create_task(hold_runtime_lease())
    await entered.wait()
    apply_task = asyncio.create_task(
        chat_plugin._apply_runtime_settings(VersionedSettings(config=candidate, revision=2))
    )
    await asyncio.sleep(0)

    assert apply_task.done() is False
    assert chat_plugin._runtime_state is original_state

    release.set()
    try:
        await holder
        await apply_task
        assert chat_plugin._runtime_state is replacement_state
    finally:
        release.set()
        await holder
        if not apply_task.done():
            apply_task.cancel()
            await asyncio.gather(apply_task, return_exceptions=True)
        chat_plugin._runtime_state = original_state
        chat_plugin._publish_runtime_state(original_state)


@pytest.mark.asyncio
async def test_runtime_reload_switches_auto_speak_trigger_without_overlap(
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_state = chat_plugin._runtime_state

    class ModeService:
        def __init__(self, runs_on_interval: bool) -> None:
            self.runs_on_interval = runs_on_interval

    interval_values = runtime_config_to_plain_values(original_state.config)
    interval_values.update(
        {
            "chat_auto_speak_enabled": True,
            "chat_auto_speak_bot_id": "940000000000001",
            "chat_auto_speak_groups": ["940000000000002"],
            "chat_auto_speak_trigger_mode": "interval",
            "chat_mcp_enabled": True,
            "chat_auto_speak_mcp_code": "decision",
            "chat_group_recent_context_enabled": True,
        }
    )
    interval_config = RuntimeConfig.model_validate(interval_values)
    interval_state = replace(
        original_state,
        config=interval_config,
        auto_speak_service=ModeService(True),
    )
    message_values = dict(interval_values)
    message_values["chat_auto_speak_trigger_mode"] = "message"
    message_config = RuntimeConfig.model_validate(message_values)
    message_state = replace(
        interval_state,
        config=message_config,
        auto_speak_service=ModeService(False),
    )
    order: list[str] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    old_work_finished = asyncio.Event()

    async def hold_old_runtime() -> None:
        async with chat_plugin._runtime_lease():
            order.append("old-start")
            entered.set()
            await release.wait()
            order.append("old-finish")
            old_work_finished.set()

    async def stop_interval() -> None:
        order.append("stop-interval")

    async def start_new_source() -> None:
        assert old_work_finished.is_set()
        assert chat_plugin.auto_speak_service.runs_on_interval is False
        order.append("start-message")

    chat_plugin._runtime_state = interval_state
    chat_plugin._publish_runtime_state(interval_state)
    monkeypatch.setattr(chat_plugin, "_runtime_gate", type(chat_plugin._runtime_gate)())
    monkeypatch.setattr(chat_plugin, "_build_runtime_state", Mock(return_value=message_state))
    monkeypatch.setattr(chat_plugin, "stop_auto_speak", stop_interval)
    monkeypatch.setattr(chat_plugin, "start_auto_speak", start_new_source)
    monkeypatch.setattr(chat_plugin, "_close_runtime_state", AsyncMock())

    holder = asyncio.create_task(hold_old_runtime())
    await entered.wait()
    apply_task = asyncio.create_task(
        chat_plugin._apply_runtime_settings(
            VersionedSettings(config=message_config, revision=2)
        )
    )
    await asyncio.sleep(0)
    assert order == ["old-start", "stop-interval"]

    release.set()
    try:
        await holder
        await apply_task
        assert order == [
            "old-start",
            "stop-interval",
            "old-finish",
            "start-message",
        ]
        assert chat_plugin._runtime_state is message_state
    finally:
        release.set()
        await asyncio.gather(holder, apply_task, return_exceptions=True)
        chat_plugin._runtime_state = original_state
        chat_plugin._publish_runtime_state(original_state)


@pytest.mark.asyncio
async def test_settings_refresh_background_task_is_idempotent_and_stoppable(
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = asyncio.Event()
    await chat_plugin.stop_settings_refresh()

    async def refresh_loop() -> None:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(chat_plugin, "_settings_refresh_loop", refresh_loop)

    await chat_plugin.start_settings_refresh()
    await started.wait()
    first_task = chat_plugin._settings_refresh_task
    await chat_plugin.start_settings_refresh()

    assert first_task is not None
    assert chat_plugin._settings_refresh_task is first_task

    await chat_plugin.stop_settings_refresh()
    assert chat_plugin._settings_refresh_task is None
