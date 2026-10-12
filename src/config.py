"""NoneBot 插件配置。"""

import json
import re
import unicodedata
from datetime import timedelta
from ipaddress import IPv4Address
from typing import Literal
from urllib.parse import urlsplit

from nonebot.adapters.onebot.utils import WSUrl
from pydantic import (
    AnyUrl,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationInfo,
    field_validator,
    model_validator,
)
from pydantic.networks import IPvAnyAddress

from agent_bot.keyword_reply import (
    MAX_KEYWORD_CHARS,
    MAX_KEYWORD_REPLY_CHARS,
    MAX_KEYWORD_RULES,
    contains_disallowed_control,
    normalize_keyword,
)

_MCP_SERVICE_CODE = re.compile(r"[a-z0-9][a-z0-9_-]{0,99}")


class PluginConfig(BaseModel):
    """从 NoneBot 全局配置中读取的 dispatch-chat 配置。"""

    model_config = ConfigDict(
        extra="ignore",
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    dispatch_chat_ai_base_url: str = "http://127.0.0.1:8090/dispatch-ai"
    dispatch_chat_plain_ai_backend: Literal["java", "python"] = "java"
    dispatch_chat_agent_backend: Literal["java", "python"] = "java"
    # 独立控制媒体路径；选中 Python 后失败不自动改走其他后端。
    dispatch_chat_media_backend: Literal["java", "python"] = "java"
    dispatch_chat_python_agent_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )
    dispatch_chat_python_search_mcp_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )
    dispatch_chat_model: str = Field(
        default="Qwen/Qwen3-8B",
        min_length=1,
        max_length=255,
    )
    dispatch_chat_vision_model: str = Field(
        default="Qwen/Qwen2.5-VL-7B-Instruct",
        min_length=1,
        max_length=255,
    )
    dispatch_chat_image_model: str = Field(
        default="gpt-image-1",
        min_length=1,
        max_length=255,
    )
    dispatch_chat_system_prompt: str = Field(
        default="请直接、友好地回应用户。",
        min_length=1,
        max_length=20_000,
        repr=False,
    )
    dispatch_chat_ai_token: SecretStr | None = Field(default=None, repr=False)
    dispatch_chat_llm_max_tokens: int | None = Field(default=None, ge=1, le=128_000)
    dispatch_chat_llm_temperature: float | None = Field(
        default=None,
        ge=0,
        le=2,
        allow_inf_nan=False,
    )
    dispatch_chat_llm_top_p: float | None = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    dispatch_chat_llm_top_k: int | None = Field(default=None, ge=1, le=100)
    dispatch_chat_llm_min_p: float | None = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    dispatch_chat_llm_presence_penalty: float | None = Field(
        default=None,
        ge=-2,
        le=2,
        allow_inf_nan=False,
    )
    dispatch_chat_llm_frequency_penalty: float | None = Field(
        default=None,
        ge=-2,
        le=2,
        allow_inf_nan=False,
    )
    dispatch_chat_llm_repetition_penalty: float | None = Field(
        default=None,
        ge=1,
        le=2,
        allow_inf_nan=False,
    )
    dispatch_chat_llm_stop: list[str] | None = None
    dispatch_chat_llm_enable_thinking: bool | None = None
    dispatch_chat_llm_forward_user_name: bool = False
    # 普通 QQ 对话的 Agent 开关；关闭时使用 chatOne。
    dispatch_chat_agent_enabled: bool = True
    # 显式设置时统一工具推理轮次；未设置时保留联网与画图的原有预算。
    dispatch_chat_agent_max_steps: int | None = Field(default=None, ge=1, le=5)
    # 仅保存 dispatch-ai 已登记的通用 MCP 服务 code，不接受端点或凭据。
    dispatch_chat_mcp_services: list[str] = Field(default_factory=list, max_length=3)
    # 仅控制 Agent 是否挂载受限联网搜索工具。
    dispatch_chat_web_search_enabled: bool = True
    dispatch_chat_web_search_max_steps: int = Field(default=3, ge=1, le=5)
    dispatch_chat_image_recognition_enabled: bool = True
    dispatch_chat_max_images_per_message: int = Field(default=4, ge=1, le=8)
    dispatch_chat_image_generation_enabled: bool = False
    dispatch_chat_image_timeout_seconds: float = Field(
        default=60,
        ge=1,
        le=120,
        allow_inf_nan=False,
    )
    dispatch_chat_image_response_format: Literal["png", "jpeg", "webp"] = "png"
    dispatch_chat_image_max_bytes: int = Field(
        default=10_485_760,
        ge=1024,
        le=10_485_760,
    )
    dispatch_chat_local_workflow_enabled: bool = False
    dispatch_chat_local_workflow_base_url: str = "http://127.0.0.1:8188"
    dispatch_chat_local_workflow_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
    )
    dispatch_chat_local_workflow_default_key: str = Field(
        default="sdxl-lightning-4step",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9-]{0,127}$",
    )
    dispatch_chat_local_workflow_timeout_seconds: float = Field(
        default=500,
        ge=10,
        le=500,
        allow_inf_nan=False,
    )
    dispatch_chat_keyword_at_replies: dict[str, str] = Field(
        default_factory=dict,
        repr=False,
    )
    dispatch_chat_auto_mention_enabled: bool = True
    dispatch_chat_mention_tool_enabled: bool = True
    dispatch_chat_tts_enabled: bool = False
    dispatch_chat_tts_url: str | None = Field(default=None, max_length=2048)
    dispatch_chat_tts_token: SecretStr | None = Field(default=None, repr=False)
    dispatch_chat_tts_model: str = Field(default="tts-1", min_length=1, max_length=255)
    dispatch_chat_tts_voice: str = Field(default="alloy", min_length=1, max_length=255)
    dispatch_chat_tts_response_format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] = "mp3"
    dispatch_chat_tts_speed: float = Field(default=1, ge=0.25, le=4, allow_inf_nan=False)
    dispatch_chat_tts_timeout_seconds: float = Field(
        default=30,
        ge=1,
        le=120,
        allow_inf_nan=False,
    )
    dispatch_chat_tts_max_input_chars: int = Field(default=1000, ge=1, le=4096)
    dispatch_chat_tts_max_audio_bytes: int = Field(
        default=5_242_880,
        ge=1024,
        le=10_485_760,
    )
    dispatch_chat_tts_reference_audio_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
    )
    dispatch_chat_tts_reference_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
        repr=False,
    )
    dispatch_chat_tts_reference_language: Literal["zh", "en", "ja", "yue", "ko"] = "zh"
    dispatch_chat_tts_text_language: Literal["zh", "en", "ja", "yue", "ko"] = "zh"
    dispatch_chat_tts_output_directory: str = Field(
        default="data/tts",
        min_length=1,
        max_length=1024,
    )
    dispatch_chat_mcp_enabled: bool = False
    # 仅向 chatAgentOne 声明 Laya；地址和长期认证令牌由 dispatch-ai 管理。
    dispatch_chat_laya_enabled: bool = False
    dispatch_chat_job_enabled: bool = False
    dispatch_chat_soloncode_enabled: bool = False
    dispatch_chat_job_poll_seconds: float = Field(default=5, ge=1, le=60, allow_inf_nan=False)
    dispatch_chat_mcp_mount_path: str = "/mcp"
    dispatch_chat_mcp_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1:*", "localhost:*", "dispatch-chat:*"],
        min_length=1,
        max_length=20,
    )
    dispatch_chat_mcp_session_ttl_seconds: float = Field(default=120, ge=1, le=300)
    dispatch_chat_mcp_max_sessions: int = Field(default=1000, ge=1, le=10_000)
    onebot_v11_access_token: SecretStr = Field(repr=False)
    dispatch_chat_session_secret: SecretStr = Field(repr=False)
    dispatch_chat_naraka_cookie: SecretStr | None = Field(default=None, repr=False)
    dispatch_chat_naraka_heybox_id: SecretStr | None = Field(default=None, repr=False)
    dispatch_chat_naraka_default_role_id: SecretStr | None = Field(
        default=None,
        repr=False,
    )
    dispatch_chat_naraka_server: str = "163"
    dispatch_chat_naraka_season: str = "qianji"
    dispatch_chat_naraka_battle_tid: str = "5000001"

    dispatch_chat_auto_speak_enabled: bool = False
    dispatch_chat_auto_speak_bot_id: SecretStr | None = Field(default=None, repr=False)
    # The scalar union lets NoneBot's settings source pass non-JSON comma lists to
    # our before-validator instead of rejecting them before PluginConfig sees them.
    dispatch_chat_auto_speak_groups: set[str] | str | int = Field(
        default_factory=set,
        repr=False,
    )
    dispatch_chat_auto_speak_users: set[str] | str | int = Field(
        default_factory=set,
        repr=False,
    )
    dispatch_chat_auto_speak_trigger_mode: Literal["interval", "message"] = "interval"
    dispatch_chat_auto_speak_prompt: str = Field(
        default="生成一句自然、友好、适合主动发给当前会话的简短消息。",
        min_length=1,
        max_length=1000,
        repr=False,
    )
    dispatch_chat_auto_speak_laya_prompt: str = Field(
        default=(
            "结合自动发言会话历史和当前群聊，判断现在是否适合主动发言；"
            "仅当发言自然、有明确价值且不会打扰群聊时选择 speak，否则选择 silent。"
        ),
        min_length=1,
        max_length=1000,
        repr=False,
    )
    dispatch_chat_auto_speak_initial_delay_seconds: float = Field(
        default=60,
        ge=10,
        le=86400,
    )
    dispatch_chat_auto_speak_interval_seconds: float = Field(
        default=3600,
        gt=0,
        le=2592000,
    )
    dispatch_chat_auto_speak_max_reply_chars: int = Field(default=500, ge=1, le=1000)

    dispatch_chat_timeout_seconds: float = Field(default=300, gt=0)
    dispatch_chat_max_input_chars: int = Field(default=2000, gt=0)
    dispatch_chat_history_turns: int = Field(default=6, ge=0)
    dispatch_chat_history_max_chars: int = Field(default=12000, gt=0)
    dispatch_chat_history_ttl_seconds: float = Field(default=604_800, gt=0)
    dispatch_chat_group_recent_context_enabled: bool = False
    dispatch_chat_group_recent_context_messages: int = Field(default=10, ge=1)
    dispatch_chat_group_recent_context_max_chars: int = Field(
        default=4000,
        ge=256,
        le=12000,
    )
    dispatch_chat_group_recent_context_ttl_seconds: float = Field(
        default=604_800,
        ge=10,
    )
    dispatch_chat_memory_enabled: bool = True
    dispatch_chat_tagged_memory_enabled: bool = True
    dispatch_chat_memory_max_chars: int = Field(default=4000, ge=256, le=8000)
    dispatch_chat_memory_compression_max_tokens: int = Field(
        default=512,
        ge=64,
        le=4096,
    )
    dispatch_chat_memory_compression_queue_capacity: int = Field(default=64, ge=1, le=1000)
    dispatch_chat_db_pool_size: int = Field(default=8, ge=1, le=64)
    dispatch_chat_db_timeout_seconds: int = Field(default=10, ge=1, le=60)
    dispatch_chat_model_cache_ttl_seconds: float = Field(
        default=5,
        ge=0,
        le=30,
        allow_inf_nan=False,
    )
    dispatch_chat_max_sessions: int = Field(default=1000, gt=0)
    dispatch_chat_max_concurrent_requests: int = Field(default=4, gt=0)
    dispatch_chat_cooldown_seconds: float = Field(default=2, ge=0)
    dispatch_chat_chunk_chars: int = Field(default=1200, gt=0)
    dispatch_chat_max_reply_chars: int = Field(default=6000, gt=0)
    dispatch_chat_queue_max_pending: int = Field(default=1000, ge=1, le=100_000)
    dispatch_chat_queue_workers: int = Field(default=4, ge=1, le=64)
    dispatch_chat_queue_media_workers: int = Field(default=1, ge=1, le=16)
    dispatch_chat_queue_max_attempts: int = Field(default=3, ge=1, le=10)
    dispatch_chat_queue_retry_initial_seconds: float = Field(
        default=2,
        gt=0,
        le=300,
        allow_inf_nan=False,
    )
    dispatch_chat_queue_retry_max_seconds: float = Field(
        default=30,
        gt=0,
        le=300,
        allow_inf_nan=False,
    )
    dispatch_chat_queue_job_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    dispatch_chat_queue_result_ttl_seconds: int = Field(
        default=1800,
        ge=60,
        le=86_400,
    )
    dispatch_chat_naraka_timeout_seconds: float = Field(default=15, gt=0)
    dispatch_chat_naraka_cooldown_seconds: float = Field(default=10, ge=0)
    dispatch_chat_naraka_max_concurrent_requests: int = Field(default=2, gt=0)

    dispatch_chat_allowed_groups: set[str] = Field(default_factory=set, repr=False)
    dispatch_chat_allowed_users: set[str] = Field(default_factory=set, repr=False)
    dispatch_chat_blocked_groups: set[str] = Field(default_factory=set, repr=False)
    dispatch_chat_blocked_users: set[str] = Field(default_factory=set, repr=False)
    dispatch_chat_allow_all: bool = False
    dispatch_chat_group_shared_session: bool = False
    dispatch_chat_priority: int = Field(default=50, ge=1)
    dispatch_chat_reset_priority: int = Field(default=10, ge=1)

    dispatch_chat_user_message_limit: int = Field(default=0, ge=0)
    dispatch_chat_user_message_window_seconds: float = Field(default=86400, gt=0)
    dispatch_chat_config_refresh_interval_seconds: float = Field(
        default=5,
        ge=1,
        le=300,
    )

    @field_validator("dispatch_chat_auto_speak_bot_id", mode="before")
    @classmethod
    def normalize_auto_speak_bot_id(cls, value: object) -> object:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator(
        "dispatch_chat_auto_speak_groups",
        "dispatch_chat_auto_speak_users",
        mode="before",
    )
    @classmethod
    def normalize_auto_speak_targets(cls, value: object) -> object:
        if value is None:
            return set()
        if isinstance(value, int) and not isinstance(value, bool):
            return {str(value)}
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                return set()
            if raw.startswith("["):
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    value = [raw]
            else:
                value = raw.split(",")
        if not isinstance(value, (list, tuple, set, frozenset)):
            return value

        normalized: set[object] = set()
        for item in value:
            if isinstance(item, int) and not isinstance(item, bool):
                normalized.add(str(item))
            elif isinstance(item, str):
                normalized.add(item.strip())
            else:
                normalized.add(item)
        return normalized

    @field_validator("dispatch_chat_auto_speak_bot_id")
    @classmethod
    def validate_auto_speak_bot_id(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        if not re.fullmatch(r"[1-9][0-9]{4,19}", value.get_secret_value()):
            raise ValueError("invalid auto-speak bot ID")
        return value

    @field_validator(
        "dispatch_chat_auto_speak_groups",
        "dispatch_chat_auto_speak_users",
    )
    @classmethod
    def validate_auto_speak_targets(cls, value: set[str]) -> set[str]:
        if any(not re.fullmatch(r"[1-9][0-9]{4,19}", target) for target in value):
            raise ValueError("invalid auto-speak target ID")
        return value

    @field_validator("dispatch_chat_ai_base_url")
    @classmethod
    def validate_ai_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("dispatch_chat_ai_base_url must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("dispatch_chat_ai_base_url cannot contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("dispatch_chat_ai_base_url cannot contain query or fragment")
        return normalized

    @field_validator("dispatch_chat_local_workflow_base_url")
    @classmethod
    def validate_local_workflow_base_url(cls, value: str) -> str:
        """只接受不携带凭据和查询参数的固定 Comfy HTTP(S) 根地址。"""

        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("local workflow base URL must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("local workflow base URL cannot contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("local workflow base URL cannot contain query or fragment")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("local workflow base URL contains an invalid port") from exc
        return normalized

    @field_validator("dispatch_chat_local_workflow_directory", mode="before")
    @classmethod
    def blank_local_workflow_directory_is_none(cls, value: object) -> object:
        """允许数据库用空值表示不执行启动期目录同步。"""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("dispatch_chat_local_workflow_directory")
    @classmethod
    def validate_local_workflow_directory(cls, value: str | None) -> str | None:
        """拒绝含控制字符的管理员工作流目录。"""

        if value is not None and any(
            unicodedata.category(character).startswith("C") for character in value
        ):
            raise ValueError("local workflow directory contains control characters")
        return value

    @field_validator("dispatch_chat_mcp_mount_path")
    @classmethod
    def validate_mcp_mount_path(cls, value: str) -> str:
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9/_-]{0,63}", value) or value.endswith("/"):
            raise ValueError("dispatch_chat_mcp_mount_path must be a bounded absolute path")
        return value

    @field_validator("dispatch_chat_mcp_allowed_hosts")
    @classmethod
    def validate_mcp_allowed_hosts(cls, value: list[str]) -> list[str]:
        if any(
            not host or len(host) > 255 or any(character in host for character in "\r\n\0")
            for host in value
        ):
            raise ValueError("dispatch_chat_mcp_allowed_hosts contains an invalid host")
        if len(set(value)) != len(value):
            raise ValueError("dispatch_chat_mcp_allowed_hosts contains duplicates")
        return value

    @field_validator(
        "dispatch_chat_model",
        "dispatch_chat_vision_model",
        "dispatch_chat_image_model",
    )
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
            raise ValueError("model ID contains unsupported control characters")
        return value

    @field_validator("dispatch_chat_llm_stop")
    @classmethod
    def validate_llm_stop(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not 1 <= len(value) <= 4:
            raise ValueError("dispatch_chat_llm_stop must contain between 1 and 4 values")
        if any(not item.strip() or len(item) > 256 for item in value):
            raise ValueError("dispatch_chat_llm_stop contains an invalid value")
        return value

    @field_validator("dispatch_chat_mcp_services")
    @classmethod
    def validate_mcp_services(cls, value: list[str]) -> list[str]:
        """限制通用 MCP 服务 code，并保留 Laya 的专用授权开关。"""

        normalized = [item.strip() for item in value]
        if any(
            not _MCP_SERVICE_CODE.fullmatch(item) or item == "laya" for item in normalized
        ) or len(set(normalized)) != len(normalized):
            raise ValueError("MCP service codes are invalid or duplicated")
        return normalized

    @field_validator("dispatch_chat_tts_url", mode="before")
    @classmethod
    def blank_tts_url_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("dispatch_chat_tts_token", mode="before")
    @classmethod
    def blank_tts_token_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("dispatch_chat_tts_url")
    @classmethod
    def validate_tts_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in value
        ):
            raise ValueError("dispatch_chat_tts_url contains unsafe characters")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("dispatch_chat_tts_url must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("dispatch_chat_tts_url cannot contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("dispatch_chat_tts_url cannot contain query or fragment")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("dispatch_chat_tts_url contains an invalid port") from exc
        return value

    @field_validator("dispatch_chat_tts_model", "dispatch_chat_tts_voice")
    @classmethod
    def validate_tts_identifier(cls, value: str) -> str:
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ValueError("TTS model and voice cannot contain control characters")
        return value

    @field_validator(
        "dispatch_chat_tts_reference_audio_path",
        "dispatch_chat_tts_reference_text",
        "dispatch_chat_tts_output_directory",
    )
    @classmethod
    def validate_tts_local_value(cls, value: str | None) -> str | None:
        if value is not None and any(
            unicodedata.category(character).startswith("C") for character in value
        ):
            raise ValueError("GPT-SoVITS setting contains control characters")
        return value

    @field_validator("dispatch_chat_keyword_at_replies")
    @classmethod
    def validate_keyword_at_replies(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > MAX_KEYWORD_RULES:
            raise ValueError(f"keyword replies support at most {MAX_KEYWORD_RULES} rules")

        normalized_rules: dict[str, str] = {}
        normalized_keywords: set[str] = set()
        for raw_keyword, raw_reply in value.items():
            keyword = raw_keyword.strip()
            reply = raw_reply.strip()
            comparable_keyword = normalize_keyword(keyword)
            if (
                not keyword
                or len(keyword) > MAX_KEYWORD_CHARS
                or contains_disallowed_control(keyword)
            ):
                raise ValueError("keyword reply contains an invalid keyword")
            if (
                not reply
                or len(reply) > MAX_KEYWORD_REPLY_CHARS
                or contains_disallowed_control(reply, allow_layout=True)
            ):
                raise ValueError("keyword reply contains invalid reply text")
            if comparable_keyword in normalized_keywords:
                raise ValueError("keyword reply contains duplicate normalized keywords")
            normalized_keywords.add(comparable_keyword)
            normalized_rules[keyword] = reply
        return normalized_rules

    @field_validator("dispatch_chat_session_secret")
    @classmethod
    def validate_session_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("dispatch_chat_session_secret must contain at least 32 characters")
        return value

    @field_validator("dispatch_chat_auto_speak_interval_seconds")
    @classmethod
    def validate_auto_speak_interval(
        cls,
        value: float,
        info: ValidationInfo,
    ) -> float:
        if info.data.get("dispatch_chat_auto_speak_trigger_mode") == "interval" and value < 60:
            raise ValueError("interval auto-speak requires at least 60 seconds")
        return value

    @field_validator("onebot_v11_access_token")
    @classmethod
    def validate_onebot_access_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("onebot_v11_access_token must contain at least 32 characters")
        return value

    @field_validator(
        "dispatch_chat_naraka_cookie",
        "dispatch_chat_naraka_heybox_id",
        "dispatch_chat_naraka_default_role_id",
        mode="before",
    )
    @classmethod
    def blank_optional_naraka_value_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator("dispatch_chat_naraka_cookie")
    @classmethod
    def validate_naraka_cookie(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        cookie = value.get_secret_value()
        if not cookie or len(cookie) > 8192 or any(char in cookie for char in "\r\n\0"):
            raise ValueError("invalid Naraka cookie")
        return value

    @field_validator("dispatch_chat_naraka_heybox_id")
    @classmethod
    def validate_naraka_heybox_id(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        account_id = value.get_secret_value()
        if not account_id.isdigit() or len(account_id) > 32:
            raise ValueError("invalid Naraka account ID")
        return value

    @field_validator("dispatch_chat_naraka_default_role_id")
    @classmethod
    def validate_naraka_default_role_id(
        cls,
        value: SecretStr | None,
    ) -> SecretStr | None:
        if value is None:
            return None
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,64}", value.get_secret_value()):
            raise ValueError("invalid Naraka default role ID")
        return value

    @field_validator(
        "dispatch_chat_naraka_server",
        "dispatch_chat_naraka_season",
        "dispatch_chat_naraka_battle_tid",
        mode="before",
    )
    @classmethod
    def validate_naraka_query_identifier(cls, value: object) -> str:
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise ValueError("invalid Naraka query identifier")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
            raise ValueError("invalid Naraka query identifier")
        return value

    @model_validator(mode="after")
    def validate_reply_limits(self) -> "PluginConfig":
        if self.dispatch_chat_chunk_chars > self.dispatch_chat_max_reply_chars:
            raise ValueError("dispatch_chat_chunk_chars cannot exceed max reply chars")
        if self.dispatch_chat_reset_priority >= self.dispatch_chat_priority:
            raise ValueError("reset priority must be lower than chat priority")
        if (
            self.dispatch_chat_queue_retry_max_seconds
            < self.dispatch_chat_queue_retry_initial_seconds
        ):
            raise ValueError("queue retry maximum cannot be less than initial delay")
        if self.dispatch_chat_queue_result_ttl_seconds < self.dispatch_chat_queue_job_ttl_seconds:
            raise ValueError("queue result TTL cannot be less than job TTL")
        if (
            self.onebot_v11_access_token.get_secret_value()
            == self.dispatch_chat_session_secret.get_secret_value()
        ):
            raise ValueError("OneBot access token and session secret must be different")
        has_cookie = self.dispatch_chat_naraka_cookie is not None
        has_account_id = self.dispatch_chat_naraka_heybox_id is not None
        if has_cookie != has_account_id:
            raise ValueError("Naraka credentials must be configured together")
        if has_cookie:
            assert self.dispatch_chat_naraka_cookie is not None
            cookie = self.dispatch_chat_naraka_cookie.get_secret_value()
            cookie_names = {
                part.partition("=")[0].strip() for part in cookie.split(";") if "=" in part
            }
            required = {"pkey", "x_heybox_id", "x_pkey", "x_xhh_tokenid"}
            if not required.issubset(cookie_names):
                raise ValueError("Naraka cookie is missing required fields")
        auto_groups = self.dispatch_chat_auto_speak_groups
        auto_users = self.dispatch_chat_auto_speak_users
        assert isinstance(auto_groups, set)
        assert isinstance(auto_users, set)
        if len(auto_groups) > 20:
            raise ValueError("auto-speak supports at most 20 group targets")
        if self.dispatch_chat_auto_speak_enabled:
            if self.dispatch_chat_auto_speak_bot_id is None:
                raise ValueError("auto-speak bot ID is required when enabled")
            if not auto_groups:
                raise ValueError("auto-speak requires at least one group target")
            if not self.dispatch_chat_mcp_enabled:
                raise ValueError("auto-speak requires Chat MCP to be enabled")
            if not self.dispatch_chat_laya_enabled:
                raise ValueError("auto-speak requires Laya to be enabled")
            if not self.dispatch_chat_group_recent_context_enabled:
                raise ValueError("auto-speak requires recent group context to be enabled")
            if self.dispatch_chat_max_concurrent_requests < 2:
                raise ValueError(
                    "auto-speak requires at least 2 concurrent AI requests "
                    "to preserve interactive chat capacity"
                )
        if self.dispatch_chat_local_workflow_enabled:
            if not self.dispatch_chat_image_generation_enabled:
                raise ValueError("local workflow requires image generation to be enabled")
            if not self.dispatch_chat_mcp_enabled:
                raise ValueError("local workflow requires MCP to be enabled")
        if self.dispatch_chat_laya_enabled and not self.dispatch_chat_mcp_enabled:
            raise ValueError("Laya requires Chat MCP to be enabled")
        if self.dispatch_chat_laya_enabled and len(self.dispatch_chat_mcp_services) >= 3:
            raise ValueError("MCP services including Laya cannot exceed three")
        if self.dispatch_chat_job_enabled and (
            not self.dispatch_chat_mcp_enabled
            or self.dispatch_chat_ai_token is None
            or len(self.dispatch_chat_ai_token.get_secret_value()) < 32
        ):
            raise ValueError(
                "Job requires Chat MCP and an AI service token of at least 32 characters"
            )
        if self.dispatch_chat_soloncode_enabled and not self.dispatch_chat_job_enabled:
            raise ValueError("SolonCode requires Job result delivery to be enabled")
        return self


class RuntimeConfig(PluginConfig):
    """完整的数据库运行时配置，包括 NoneBot 启动所需字段。"""

    driver: str = Field(default="~fastapi", min_length=1)
    host: IPvAnyAddress = IPv4Address("127.0.0.1")  # type: ignore[assignment]
    port: int = Field(default=8080, ge=1, le=65535)
    log_level: int | str = "INFO"
    api_timeout: float | None = Field(default=30, gt=0)
    superusers: set[str] = Field(default_factory=set, repr=False)
    nickname: set[str] = Field(default_factory=set, repr=False)
    command_start: set[str] = Field(default_factory=lambda: {"/"})
    command_sep: set[str] = Field(default_factory=lambda: {"."})
    session_expire_timeout: timedelta = Field(
        default=timedelta(minutes=2),
        gt=timedelta(0),
    )

    onebot_v11_secret: SecretStr | None = Field(default=None, repr=False)
    onebot_v11_ws_urls: set[WSUrl] = Field(default_factory=set, repr=False)
    onebot_v11_api_roots: dict[str, AnyUrl] = Field(default_factory=dict, repr=False)

    @field_validator("onebot_v11_secret", mode="before")
    @classmethod
    def blank_optional_onebot_secret_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("onebot_v11_ws_urls")
    @classmethod
    def reject_onebot_ws_url_userinfo(cls, values: set[WSUrl]) -> set[WSUrl]:
        if any(
            value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
            for value in values
        ):
            raise ValueError("onebot_v11_ws_urls cannot contain userinfo, query, or fragment")
        return values

    @field_validator("onebot_v11_api_roots")
    @classmethod
    def reject_onebot_api_root_userinfo(
        cls,
        values: dict[str, AnyUrl],
    ) -> dict[str, AnyUrl]:
        if any(
            value.username is not None
            or value.password is not None
            or value.query is not None
            or value.fragment is not None
            for value in values.values()
        ):
            raise ValueError("onebot_v11_api_roots cannot contain userinfo, query, or fragment")
        return values


CONFIG_DESCRIPTIONS: dict[str, str] = {
    # AI 服务
    "dispatch_chat_ai_base_url": "dispatch-ai 服务地址",
    "dispatch_chat_plain_ai_backend": "普通文本 AI 后端 (java/python)",
    "dispatch_chat_agent_backend": "Agent AI 后端 (java/python)",
    "dispatch_chat_media_backend": "媒体 AI 后端 (java/python；默认 java，失败不自动切换)",
    "dispatch_chat_python_agent_code": "Python Agent 预设代码 (空值使用当前聊天模型与提示词)",
    "dispatch_chat_python_search_mcp_code": "Python 网页搜索 MCP 服务代码 (search 分类)",
    "dispatch_chat_model": "聊天模型名称",
    "dispatch_chat_vision_model": "图片理解模型名称",
    "dispatch_chat_image_model": "图片生成模型名称",
    "dispatch_chat_system_prompt": "系统提示词",
    "dispatch_chat_ai_token": "AI 服务鉴权令牌",
    "dispatch_chat_llm_max_tokens": "LLM 最大生成 Token 数",
    "dispatch_chat_llm_temperature": "LLM Temperature 采样参数",
    "dispatch_chat_llm_top_p": "LLM Top P 采样参数",
    "dispatch_chat_llm_top_k": "LLM Top K 采样参数",
    "dispatch_chat_llm_min_p": "LLM Min P 采样参数",
    "dispatch_chat_llm_presence_penalty": "LLM 存在惩罚",
    "dispatch_chat_llm_frequency_penalty": "LLM 频率惩罚",
    "dispatch_chat_llm_repetition_penalty": "LLM 重复惩罚",
    "dispatch_chat_llm_stop": "LLM 停止序列",
    "dispatch_chat_llm_enable_thinking": "是否启用模型深度思考",
    "dispatch_chat_llm_forward_user_name": "是否额外发送当前 QQ 昵称元数据",
    "dispatch_chat_agent_enabled": "是否为普通 QQ 对话启用 Agent 模式",
    "dispatch_chat_agent_max_steps": "普通聊天 Agent 最大工具推理轮次 (1-5，空值沿用原预算)",
    "dispatch_chat_mcp_services": "普通聊天可使用的通用 MCP 服务 code 列表",
    "dispatch_chat_web_search_enabled": "是否为普通 QQ 对话启用受限搜索和网页读取",
    "dispatch_chat_web_search_max_steps": "联网搜索 Agent 最大推理步数",
    "dispatch_chat_image_recognition_enabled": "是否识别 QQ 消息中的图片素材",
    "dispatch_chat_max_images_per_message": "单条消息最多识别的图片数量",
    "dispatch_chat_image_generation_enabled": "图片生成开关，本地工作流启用前也需开启",
    "dispatch_chat_image_timeout_seconds": "图片生成请求超时秒数",
    "dispatch_chat_image_response_format": "图片生成输出格式",
    "dispatch_chat_image_max_bytes": "单张生成图片最大字节数",
    "dispatch_chat_local_workflow_enabled": "是否使用本地 Comfy 工作流替代云端生图",
    "dispatch_chat_local_workflow_base_url": "本地 ComfyUI 服务根地址",
    "dispatch_chat_local_workflow_directory": "启动期工作流 JSON 导入目录",
    "dispatch_chat_local_workflow_default_key": "/画图命令使用的默认工作流键",
    "dispatch_chat_local_workflow_timeout_seconds": "本地 Comfy 工作流总超时秒数",
    "dispatch_chat_keyword_at_replies": "群聊关键词到 @ 回复文本的映射",
    "dispatch_chat_auto_mention_enabled": "是否自动识别群聊前 20 字内明确要求代发的 @ 人指令",
    "dispatch_chat_mention_tool_enabled": "是否允许 Agent 调用群成员 @ 工具",
    "dispatch_chat_tts_enabled": "是否授权 LLM 自主生成语音回复",
    "dispatch_chat_tts_url": "GPT-SoVITS /tts 完整接口地址",
    "dispatch_chat_tts_token": "已废弃，运行时不使用：旧 TTS 服务鉴权令牌",
    "dispatch_chat_tts_model": "原生音频生成模型名称",
    "dispatch_chat_tts_voice": "原生音频输出音色",
    "dispatch_chat_tts_response_format": "原生音频输出格式",
    "dispatch_chat_tts_speed": "GPT-SoVITS 固定语速倍率",
    "dispatch_chat_tts_timeout_seconds": "原生音频请求超时秒数",
    "dispatch_chat_tts_max_input_chars": "单次原生音频请求最大输入字符数",
    "dispatch_chat_tts_max_audio_bytes": "单次原生音频最大字节数",
    "dispatch_chat_tts_reference_audio_path": "GPT-SoVITS 固定参考音频路径",
    "dispatch_chat_tts_reference_text": "GPT-SoVITS 固定参考音频文本",
    "dispatch_chat_tts_reference_language": "GPT-SoVITS 参考文本语言",
    "dispatch_chat_tts_text_language": "GPT-SoVITS 目标文本语言",
    "dispatch_chat_tts_output_directory": "GPT-SoVITS 生成音频目录",
    "dispatch_chat_mcp_enabled": "是否启用供 dispatch-ai 调用的 Chat MCP 工具服务",
    "dispatch_chat_laya_enabled": "是否在 Chat Agent 请求中授权 Laya multilingual 决策工具",
    "dispatch_chat_job_enabled": "是否授权 AI 定时任务工具并拉取完成结果",
    "dispatch_chat_soloncode_enabled": "是否授权远程 SolonCode 工具，需先启用 Job 结果投递",
    "dispatch_chat_job_poll_seconds": "AI 任务结果拉取间隔秒数，范围 1-60",
    "dispatch_chat_mcp_mount_path": "Chat MCP 服务挂载路径",
    "dispatch_chat_mcp_allowed_hosts": "Chat MCP Host 请求白名单",
    "dispatch_chat_mcp_session_ttl_seconds": "Chat MCP 请求上下文有效秒数",
    "dispatch_chat_mcp_max_sessions": "Chat MCP 最大并发上下文数",
    "dispatch_chat_session_secret": "会话密钥 (HMAC 生成会话 ID)",
    # OneBot 连接
    "onebot_v11_access_token": "OneBot V11 访问令牌",
    "onebot_v11_secret": "OneBot WebSocket 密钥",
    "onebot_v11_ws_urls": "OneBot WebSocket 地址列表",
    "onebot_v11_api_roots": "OneBot HTTP API 根地址",
    # NoneBot 运行时
    "driver": "NoneBot 驱动",
    "host": "监听地址",
    "port": "监听端口",
    "log_level": "日志级别",
    "api_timeout": "API 超时时间 (秒)",
    "superusers": "超级用户 QQ 号列表",
    "nickname": "机器人昵称",
    "command_start": "命令前缀",
    "command_sep": "命令分隔符",
    "session_expire_timeout": "会话过期时间 (秒)",
    # 聊天行为
    "dispatch_chat_timeout_seconds": "AI 调用超时 (秒)",
    "dispatch_chat_max_input_chars": "单条消息最大输入字数",
    "dispatch_chat_history_turns": "历史对话保留轮数",
    "dispatch_chat_history_max_chars": "历史对话最大字符数",
    "dispatch_chat_history_ttl_seconds": "历史对话存活时间 (秒)",
    "dispatch_chat_group_recent_context_enabled": "是否读取当前群最近普通文本",
    "dispatch_chat_group_recent_context_messages": "当前群最近文本消息条数",
    "dispatch_chat_group_recent_context_max_chars": "当前群最近文本最大字符数",
    "dispatch_chat_group_recent_context_ttl_seconds": "当前群最近文本存活时间 (秒)",
    "dispatch_chat_memory_enabled": "是否启用会话心智记忆与自动压缩",
    "dispatch_chat_tagged_memory_enabled": "是否启用由 LLM 自主存取的标签记忆工具",
    "dispatch_chat_memory_max_chars": "单会话心智记忆最大字符数",
    "dispatch_chat_memory_compression_max_tokens": "记忆压缩最大生成 Token 数",
    "dispatch_chat_memory_compression_queue_capacity": "后台记忆压缩最多等待会话数",
    "dispatch_chat_db_pool_size": "聊天数据库共享连接池容量 (重启生效)",
    "dispatch_chat_db_timeout_seconds": "聊天数据库连接等待与读写超时秒数 (重启生效)",
    "dispatch_chat_model_cache_ttl_seconds": "模型启用状态缓存秒数 (Java 仍执行权威校验)",
    "dispatch_chat_max_sessions": "最大并发会话数",
    "dispatch_chat_max_concurrent_requests": "最大并发 AI 请求数",
    "dispatch_chat_cooldown_seconds": "非排队聊天冷却时间 (秒)",
    "dispatch_chat_chunk_chars": "回复分段字符数",
    "dispatch_chat_max_reply_chars": "单次回复最大字符数",
    "dispatch_chat_queue_max_pending": "Redis 对话队列最大待处理任务数",
    "dispatch_chat_queue_workers": "Redis 对话队列文本并发工作数",
    "dispatch_chat_queue_media_workers": "Redis 对话队列媒体并发工作数",
    "dispatch_chat_queue_max_attempts": "对话任务最大尝试次数",
    "dispatch_chat_queue_retry_initial_seconds": "对话任务首次重试等待秒数",
    "dispatch_chat_queue_retry_max_seconds": "对话任务最大重试等待秒数",
    "dispatch_chat_queue_job_ttl_seconds": "对话任务最长存活秒数",
    "dispatch_chat_queue_result_ttl_seconds": "已生成待发送结果存活秒数",
    # 权限
    "dispatch_chat_allowed_groups": "私聊白名单群列表",
    "dispatch_chat_allowed_users": "私聊白名单用户列表",
    "dispatch_chat_blocked_groups": "群聊黑名单列表",
    "dispatch_chat_blocked_users": "用户黑名单列表",
    "dispatch_chat_allow_all": "是否允许所有私聊",
    "dispatch_chat_group_shared_session": "群聊是否共享会话",
    "dispatch_chat_priority": "聊天命令优先级",
    "dispatch_chat_reset_priority": "重置命令优先级",
    # 消息限制
    "dispatch_chat_user_message_limit": "用户消息条数限制 (0=不限制)",
    "dispatch_chat_user_message_window_seconds": "消息限制时间窗口 (秒)",
    "dispatch_chat_config_refresh_interval_seconds": "配置自动刷新间隔 (秒)",
    # 永劫无间
    "dispatch_chat_naraka_cookie": "永劫无间 Heybox Cookie",
    "dispatch_chat_naraka_heybox_id": "永劫无间 Heybox 账号 ID",
    "dispatch_chat_naraka_default_role_id": "永劫无间默认角色 ID",
    "dispatch_chat_naraka_server": "永劫无间服务器",
    "dispatch_chat_naraka_season": "旧版兼容；当前赛季从响应读取",
    "dispatch_chat_naraka_battle_tid": "旧版兼容；当前战斗类型从响应读取",
    "dispatch_chat_naraka_timeout_seconds": "永劫查询超时 (秒)",
    "dispatch_chat_naraka_cooldown_seconds": "永劫查询冷却时间 (秒)",
    "dispatch_chat_naraka_max_concurrent_requests": "永劫查询最大并发数",
    # 自动发言
    "dispatch_chat_auto_speak_enabled": "是否启用自动发言",
    "dispatch_chat_auto_speak_bot_id": "自动发言机器人 QQ 号",
    "dispatch_chat_auto_speak_groups": "自动发言目标群列表",
    "dispatch_chat_auto_speak_users": "已废弃的自动发言目标用户列表 (仅兼容读取，不再生效)",
    "dispatch_chat_auto_speak_trigger_mode": "自动发言触发模式 (interval 或 message)",
    "dispatch_chat_auto_speak_prompt": "自动发言提示词",
    "dispatch_chat_auto_speak_laya_prompt": "自动发言 Laya 判断指令",
    "dispatch_chat_auto_speak_initial_delay_seconds": "自动发言初始延迟 (秒)",
    "dispatch_chat_auto_speak_interval_seconds": "自动发言间隔 (秒)",
    "dispatch_chat_auto_speak_max_reply_chars": "自动发言最大回复字数",
}
