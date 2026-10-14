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
    """从 NoneBot 全局配置中读取的 chat 配置。"""

    model_config = ConfigDict(
        extra="ignore",
        hide_input_in_errors=True,
        str_strip_whitespace=True,
    )

    chat_python_agent_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )
    chat_python_search_mcp_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )
    chat_model: str = Field(
        default="Qwen/Qwen3-8B",
        min_length=1,
        max_length=255,
    )
    chat_vision_model: str = Field(
        default="Qwen/Qwen2.5-VL-7B-Instruct",
        min_length=1,
        max_length=255,
    )
    chat_image_model: str = Field(
        default="gpt-image-1",
        min_length=1,
        max_length=255,
    )
    chat_system_prompt: str = Field(
        default="请直接、友好地回应用户。",
        max_length=20_000,
        repr=False,
    )
    chat_llm_max_tokens: int | None = Field(default=None, ge=1, le=128_000)
    chat_llm_temperature: float | None = Field(
        default=None,
        ge=0,
        le=2,
        allow_inf_nan=False,
    )
    chat_llm_top_p: float | None = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    chat_llm_top_k: int | None = Field(default=None, ge=1, le=100)
    chat_llm_min_p: float | None = Field(
        default=None,
        ge=0,
        le=1,
        allow_inf_nan=False,
    )
    chat_llm_presence_penalty: float | None = Field(
        default=None,
        ge=-2,
        le=2,
        allow_inf_nan=False,
    )
    chat_llm_frequency_penalty: float | None = Field(
        default=None,
        ge=-2,
        le=2,
        allow_inf_nan=False,
    )
    chat_llm_repetition_penalty: float | None = Field(
        default=None,
        ge=1,
        le=2,
        allow_inf_nan=False,
    )
    chat_llm_stop: list[str] | None = None
    chat_llm_enable_thinking: bool | None = None
    chat_llm_forward_user_name: bool = False
    # 显式设置时统一工具推理轮次；未设置时保留联网与画图的原有预算。
    chat_agent_max_steps: int | None = Field(default=None, ge=1, le=5)
    # 仅保存本项目已登记的通用 MCP 服务 code，不接受端点或凭据。
    chat_mcp_services: list[str] = Field(default_factory=list, max_length=3)
    # 仅控制 Agent 是否挂载受限联网搜索工具。
    chat_web_search_enabled: bool = True
    chat_web_search_max_steps: int = Field(default=3, ge=1, le=5)
    chat_image_recognition_enabled: bool = True
    chat_max_images_per_message: int = Field(default=4, ge=1, le=8)
    chat_image_generation_enabled: bool = False
    chat_image_timeout_seconds: float = Field(
        default=60,
        ge=1,
        le=120,
        allow_inf_nan=False,
    )
    chat_image_response_format: Literal["png", "jpeg", "webp"] = "png"
    chat_image_max_bytes: int = Field(
        default=10_485_760,
        ge=1024,
        le=10_485_760,
    )
    chat_local_workflow_enabled: bool = False
    chat_local_workflow_base_url: str = "http://127.0.0.1:8188"
    chat_local_workflow_directory: str | None = Field(
        default=None,
        min_length=1,
        max_length=1024,
    )
    chat_local_workflow_default_key: str = Field(
        default="sdxl-lightning-4step",
        min_length=1,
        max_length=128,
        pattern=r"^[a-z0-9][a-z0-9-]{0,127}$",
    )
    chat_local_workflow_timeout_seconds: float = Field(
        default=500,
        ge=10,
        le=500,
        allow_inf_nan=False,
    )
    chat_keyword_at_replies: dict[str, str] = Field(
        default_factory=dict,
        repr=False,
    )
    chat_auto_mention_enabled: bool = True
    chat_mention_tool_enabled: bool = True
    chat_tts_enabled: bool = False
    chat_tts_url: str | None = Field(default=None, max_length=2048)
    chat_tts_token: SecretStr | None = Field(default=None, repr=False)
    chat_tts_model: str = Field(default="tts-1", min_length=1, max_length=255)
    chat_tts_voice: str = Field(default="alloy", min_length=1, max_length=255)
    chat_tts_response_format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] = "mp3"
    chat_tts_speed: float = Field(default=1, ge=0.25, le=4, allow_inf_nan=False)
    chat_tts_timeout_seconds: float = Field(
        default=30,
        ge=1,
        le=120,
        allow_inf_nan=False,
    )
    chat_tts_max_input_chars: int = Field(default=1000, ge=1, le=4096)
    chat_tts_max_audio_bytes: int = Field(
        default=5_242_880,
        ge=1024,
        le=10_485_760,
    )
    chat_tts_reference_audio_path: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
    )
    chat_tts_reference_text: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
        repr=False,
    )
    chat_tts_reference_language: Literal["zh", "en", "ja", "yue", "ko"] = "zh"
    chat_tts_text_language: Literal["zh", "en", "ja", "yue", "ko"] = "zh"
    chat_tts_output_directory: str = Field(
        default="data/tts",
        min_length=1,
        max_length=1024,
    )
    chat_mcp_enabled: bool = False
    chat_mcp_mount_path: str = "/mcp"
    chat_mcp_allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1:*", "localhost:*", "chat:*"],
        min_length=1,
        max_length=20,
    )
    chat_mcp_session_ttl_seconds: float = Field(default=120, ge=1, le=300)
    chat_mcp_max_sessions: int = Field(default=1000, ge=1, le=10_000)
    # Job 由本地 Python 执行，通过请求令牌绑定原 QQ 身份。
    chat_job_enabled: bool = False
    chat_job_poll_seconds: float = Field(default=5, ge=1, le=60, allow_inf_nan=False)
    # 决策端点和凭据只从管理端 MCP 目录读取，配置中只保存 code。
    chat_auto_speak_enabled: bool = False
    chat_auto_speak_mcp_code: str | None = Field(
        default=None, pattern=r"^[a-z][a-z0-9_-]{0,63}$"
    )
    chat_auto_speak_bot_id: SecretStr | None = Field(default=None, repr=False)
    chat_auto_speak_groups: set[str] | str | int = Field(default_factory=set, repr=False)
    chat_auto_speak_trigger_mode: Literal["interval", "message"] = "interval"
    chat_auto_speak_prompt: str = Field(
        default="生成一句自然、友好、适合主动发给当前会话的简短消息。",
        min_length=1, max_length=1000, repr=False,
    )
    chat_auto_speak_laya_prompt: str = Field(
        default=("结合自动发言会话历史和当前群聊，判断现在是否适合主动发言；"
                 "仅当发言自然、有明确价值且不会打扰群聊时选择 speak，否则选择 silent。"),
        min_length=1, max_length=1000, repr=False,
    )
    chat_auto_speak_initial_delay_seconds: float = Field(
        default=60, ge=10, le=86400, allow_inf_nan=False,
    )
    chat_auto_speak_interval_seconds: float = Field(
        default=3600, gt=0, le=2592000, allow_inf_nan=False,
    )
    chat_auto_speak_max_reply_chars: int = Field(default=500, ge=1, le=1000)
    onebot_v11_access_token: SecretStr = Field(repr=False)
    chat_session_secret: SecretStr = Field(repr=False)
    chat_naraka_cookie: SecretStr | None = Field(default=None, repr=False)
    chat_naraka_heybox_id: SecretStr | None = Field(default=None, repr=False)
    chat_naraka_default_role_id: SecretStr | None = Field(
        default=None,
        repr=False,
    )
    chat_naraka_server: str = "163"
    chat_naraka_season: str = "qianji"
    chat_naraka_battle_tid: str = "5000001"


    chat_timeout_seconds: float = Field(default=300, gt=0)
    chat_max_input_chars: int = Field(default=2000, gt=0)
    chat_history_turns: int = Field(default=6, ge=0)
    chat_history_max_chars: int = Field(default=12000, gt=0)
    chat_history_ttl_seconds: float = Field(default=604_800, gt=0)
    chat_group_recent_context_enabled: bool = False
    chat_group_recent_context_messages: int = Field(default=10, ge=1)
    chat_group_recent_context_max_chars: int = Field(
        default=4000,
        ge=256,
        le=12000,
    )
    chat_group_recent_context_ttl_seconds: float = Field(
        default=604_800,
        ge=10,
    )
    chat_memory_enabled: bool = True
    chat_tagged_memory_enabled: bool = True
    chat_memory_max_chars: int = Field(default=4000, ge=256, le=8000)
    chat_memory_compression_max_tokens: int = Field(
        default=512,
        ge=64,
        le=4096,
    )
    chat_memory_compression_queue_capacity: int = Field(default=64, ge=1, le=1000)
    chat_db_pool_size: int = Field(default=8, ge=1, le=64)
    chat_db_timeout_seconds: int = Field(default=10, ge=1, le=60)
    chat_max_sessions: int = Field(default=1000, gt=0)
    # 同会话并发与底层 Agent 的八个执行名额对齐，超限立即拒绝而不等待。
    chat_max_concurrent_requests: int = Field(default=8, gt=0)
    chat_cooldown_seconds: float = Field(default=2, ge=0)
    chat_chunk_chars: int = Field(default=1200, gt=0)
    chat_max_reply_chars: int = Field(default=6000, gt=0)
    chat_queue_max_pending: int = Field(default=1000, ge=1, le=100_000)
    chat_queue_workers: int = Field(default=8, ge=1, le=64)
    chat_queue_media_workers: int = Field(default=1, ge=1, le=16)
    chat_queue_max_attempts: int = Field(default=3, ge=1, le=10)
    chat_queue_retry_initial_seconds: float = Field(
        default=2,
        gt=0,
        le=300,
        allow_inf_nan=False,
    )
    chat_queue_retry_max_seconds: float = Field(
        default=30,
        gt=0,
        le=300,
        allow_inf_nan=False,
    )
    chat_queue_job_ttl_seconds: int = Field(default=900, ge=60, le=86_400)
    chat_queue_result_ttl_seconds: int = Field(
        default=1800,
        ge=60,
        le=86_400,
    )
    chat_naraka_timeout_seconds: float = Field(default=15, gt=0)
    chat_naraka_cooldown_seconds: float = Field(default=10, ge=0)
    chat_naraka_max_concurrent_requests: int = Field(default=2, gt=0)

    chat_allowed_groups: set[str] = Field(default_factory=set, repr=False)
    chat_allowed_users: set[str] = Field(default_factory=set, repr=False)
    chat_blocked_groups: set[str] = Field(default_factory=set, repr=False)
    chat_blocked_users: set[str] = Field(default_factory=set, repr=False)
    chat_allow_all: bool = False
    chat_group_shared_session: bool = False
    chat_priority: int = Field(default=50, ge=1)
    chat_reset_priority: int = Field(default=10, ge=1)

    chat_user_message_limit: int = Field(default=0, ge=0)
    chat_user_message_window_seconds: float = Field(default=86400, gt=0)
    chat_config_refresh_interval_seconds: float = Field(
        default=5,
        ge=1,
        le=300,
    )

    @field_validator("chat_mcp_services", "chat_mcp_allowed_hosts", mode="before")
    @classmethod
    def parse_mcp_json_lists(cls, value: object) -> object:
        """解析 dotenv 中的 JSON 列表，随后继续执行元素和长度约束。"""
        # NoneBot 对带长度约束的 Annotated 列表会保留字符串，需要在模型边界解码。
        return json.loads(value) if isinstance(value, str) else value

    @field_validator("chat_auto_speak_bot_id", mode="before")
    @classmethod
    def normalize_auto_speak_bot(cls, value: object) -> object:
        """允许环境文件中的空值及整数 QQ 号，不接受布尔值。"""
        if isinstance(value, str) and not value.strip():
            return None
        return str(value) if type(value) is int else value

    @field_validator("chat_auto_speak_bot_id")
    @classmethod
    def validate_auto_speak_bot(cls, value: SecretStr | None) -> SecretStr | None:
        """自动发言必须绑定一个有效机器人，避免发送到任意目标。"""
        if value is not None and not re.fullmatch(r"[1-9][0-9]{4,19}", value.get_secret_value()):
            raise ValueError("invalid auto-speak bot ID")
        return value

    @field_validator("chat_auto_speak_groups", mode="before")
    @classmethod
    def normalize_auto_speak_groups(cls, value: object) -> set[str]:
        """统一 JSON 列表和逗号分隔群号，并在配置边界逐个校验。"""
        if isinstance(value, str):
            value = json.loads(value) if value.strip().startswith("[") else value.split(",")
        elif type(value) is int:
            value = [value]
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("invalid auto-speak groups")
        groups = {str(item).strip() for item in value if str(item).strip()}
        if len(groups) > 20:
            raise ValueError("auto-speak supports at most 20 group targets")
        if any(not re.fullmatch(r"[1-9][0-9]{4,19}", item) for item in groups):
            raise ValueError("invalid auto-speak group ID")
        return groups

    @model_validator(mode="after")
    def validate_python_background_capabilities(self) -> "PluginConfig":
        """校验后台能力所需的本地授权和决策依赖。"""
        if self.chat_job_enabled and not self.chat_mcp_enabled:
            raise ValueError("Job requires chat MCP sessions")
        if (self.chat_auto_speak_trigger_mode == "interval"
                and self.chat_auto_speak_interval_seconds < 60):
            raise ValueError("auto-speak interval must be at least 60 seconds")
        if self.chat_auto_speak_enabled:
            if not (self.chat_auto_speak_bot_id and self.chat_auto_speak_groups
                    and self.chat_auto_speak_mcp_code):
                raise ValueError("auto-speak requires bot, groups and registered MCP code")
            if not self.chat_group_recent_context_enabled:
                raise ValueError("auto-speak requires recent group context")
            if self.chat_max_concurrent_requests < 2:
                raise ValueError("auto-speak requires at least 2 concurrent AI requests")
        return self

    @field_validator("chat_tts_reference_audio_path", "chat_tts_reference_text", mode="before")
    @classmethod
    def blank_tts_reference_is_none(cls, value: object) -> object:
        """示例中留空的可选参考音频配置表示未设置。"""
        return None if isinstance(value, str) and not value.strip() else value


    @field_validator("chat_local_workflow_base_url")
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

    @field_validator("chat_local_workflow_directory", mode="before")
    @classmethod
    def blank_local_workflow_directory_is_none(cls, value: object) -> object:
        """允许数据库用空值表示不执行启动期目录同步。"""

        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("chat_local_workflow_directory")
    @classmethod
    def validate_local_workflow_directory(cls, value: str | None) -> str | None:
        """拒绝含控制字符的管理员工作流目录。"""

        if value is not None and any(
            unicodedata.category(character).startswith("C") for character in value
        ):
            raise ValueError("local workflow directory contains control characters")
        return value

    @field_validator("chat_mcp_mount_path")
    @classmethod
    def validate_mcp_mount_path(cls, value: str) -> str:
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9/_-]{0,63}", value) or value.endswith("/"):
            raise ValueError("chat_mcp_mount_path must be a bounded absolute path")
        return value

    @field_validator("chat_mcp_allowed_hosts")
    @classmethod
    def validate_mcp_allowed_hosts(cls, value: list[str]) -> list[str]:
        if any(
            not host or len(host) > 255 or any(character in host for character in "\r\n\0")
            for host in value
        ):
            raise ValueError("chat_mcp_allowed_hosts contains an invalid host")
        if len(set(value)) != len(value):
            raise ValueError("chat_mcp_allowed_hosts contains duplicates")
        return value

    @field_validator(
        "chat_model",
        "chat_vision_model",
        "chat_image_model",
    )
    @classmethod
    def validate_model_id(cls, value: str) -> str:
        if any(unicodedata.category(character) in {"Cc", "Cs"} for character in value):
            raise ValueError("model ID contains unsupported control characters")
        return value

    @field_validator("chat_llm_stop")
    @classmethod
    def validate_llm_stop(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not 1 <= len(value) <= 4:
            raise ValueError("chat_llm_stop must contain between 1 and 4 values")
        if any(not item.strip() or len(item) > 256 for item in value):
            raise ValueError("chat_llm_stop contains an invalid value")
        return value

    @field_validator("chat_mcp_services")
    @classmethod
    def validate_mcp_services(cls, value: list[str]) -> list[str]:
        """限制通用 MCP 服务 code，并保留 Laya 的专用授权开关。"""

        normalized = [item.strip() for item in value]
        if any(
            not _MCP_SERVICE_CODE.fullmatch(item) or item == "laya" for item in normalized
        ) or len(set(normalized)) != len(normalized):
            raise ValueError("MCP service codes are invalid or duplicated")
        return normalized

    @field_validator("chat_tts_url", mode="before")
    @classmethod
    def blank_tts_url_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("chat_tts_token", mode="before")
    @classmethod
    def blank_tts_token_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("chat_tts_url")
    @classmethod
    def validate_tts_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(
            character.isspace() or unicodedata.category(character).startswith("C")
            for character in value
        ):
            raise ValueError("chat_tts_url contains unsafe characters")
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("chat_tts_url must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("chat_tts_url cannot contain userinfo")
        if parsed.query or parsed.fragment:
            raise ValueError("chat_tts_url cannot contain query or fragment")
        try:
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("chat_tts_url contains an invalid port") from exc
        return value

    @field_validator("chat_tts_model", "chat_tts_voice")
    @classmethod
    def validate_tts_identifier(cls, value: str) -> str:
        if any(unicodedata.category(character).startswith("C") for character in value):
            raise ValueError("TTS model and voice cannot contain control characters")
        return value

    @field_validator(
        "chat_tts_reference_audio_path",
        "chat_tts_reference_text",
        "chat_tts_output_directory",
    )
    @classmethod
    def validate_tts_local_value(cls, value: str | None) -> str | None:
        if value is not None and any(
            unicodedata.category(character).startswith("C") for character in value
        ):
            raise ValueError("GPT-SoVITS setting contains control characters")
        return value

    @field_validator("chat_keyword_at_replies")
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

    @field_validator("chat_session_secret")
    @classmethod
    def validate_session_secret(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("chat_session_secret must contain at least 32 characters")
        return value


    @field_validator("onebot_v11_access_token")
    @classmethod
    def validate_onebot_access_token(cls, value: SecretStr) -> SecretStr:
        """认证头不能包含控制或代理字符，校验错误不披露 Token 内容。"""
        plain = value.get_secret_value()
        if len(plain) < 32:
            raise ValueError("onebot_v11_access_token must contain at least 32 characters")
        if any(unicodedata.category(character) in {"Cc", "Cs"} for character in plain):
            raise ValueError("onebot_v11_access_token contains invalid control characters")
        return value

    @field_validator(
        "chat_naraka_cookie",
        "chat_naraka_heybox_id",
        "chat_naraka_default_role_id",
        mode="before",
    )
    @classmethod
    def blank_optional_naraka_value_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
        return value

    @field_validator("chat_naraka_cookie")
    @classmethod
    def validate_naraka_cookie(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        cookie = value.get_secret_value()
        if not cookie or len(cookie) > 8192 or any(char in cookie for char in "\r\n\0"):
            raise ValueError("invalid Naraka cookie")
        return value

    @field_validator("chat_naraka_heybox_id")
    @classmethod
    def validate_naraka_heybox_id(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        account_id = value.get_secret_value()
        if not account_id.isdigit() or len(account_id) > 32:
            raise ValueError("invalid Naraka account ID")
        return value

    @field_validator("chat_naraka_default_role_id")
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
        "chat_naraka_server",
        "chat_naraka_season",
        "chat_naraka_battle_tid",
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
        if self.chat_chunk_chars > self.chat_max_reply_chars:
            raise ValueError("chat_chunk_chars cannot exceed max reply chars")
        if self.chat_reset_priority >= self.chat_priority:
            raise ValueError("reset priority must be lower than chat priority")
        if (
            self.chat_queue_retry_max_seconds
            < self.chat_queue_retry_initial_seconds
        ):
            raise ValueError("queue retry maximum cannot be less than initial delay")
        if self.chat_queue_result_ttl_seconds < self.chat_queue_job_ttl_seconds:
            raise ValueError("queue result TTL cannot be less than job TTL")
        if (
            self.onebot_v11_access_token.get_secret_value()
            == self.chat_session_secret.get_secret_value()
        ):
            raise ValueError("OneBot access token and session secret must be different")
        has_cookie = self.chat_naraka_cookie is not None
        has_account_id = self.chat_naraka_heybox_id is not None
        if has_cookie != has_account_id:
            raise ValueError("Naraka credentials must be configured together")
        if has_cookie:
            assert self.chat_naraka_cookie is not None
            cookie = self.chat_naraka_cookie.get_secret_value()
            cookie_names = {
                part.partition("=")[0].strip() for part in cookie.split(";") if "=" in part
            }
            required = {"pkey", "x_heybox_id", "x_pkey", "x_xhh_tokenid"}
            if not required.issubset(cookie_names):
                raise ValueError("Naraka cookie is missing required fields")
        if self.chat_local_workflow_enabled:
            if not self.chat_image_generation_enabled:
                raise ValueError("local workflow requires image generation to be enabled")
            if not self.chat_mcp_enabled:
                raise ValueError("local workflow requires MCP to be enabled")
        return self


class RuntimeConfig(PluginConfig):
    """合并启动静态配置与数据库动态配置后的有效快照，供业务统一校验和消费。"""

    app_enabled_plugins: list[str] = Field(default_factory=lambda: ["qqbot"])

    @field_validator("app_enabled_plugins")
    @classmethod
    def validate_enabled_plugins(cls, value: list[str]) -> list[str]:
        """只允许内置插件 ID，数据库配置不能触发任意模块导入。"""
        from agent_bot.plugins.catalog import BUILTIN_PLUGIN_IDS

        if len(set(value)) != len(value) or set(value) - BUILTIN_PLUGIN_IDS:
            raise ValueError("unknown or duplicate enabled plugin ID")
        return value

    driver: str = Field(default="~fastapi+~websockets", min_length=1)
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

    @field_validator("nickname")
    @classmethod
    def normalize_bot_names(cls, values: set[str]) -> set[str]:
        """清理触发名称的首尾空白并去重，空列表表示仅靠 At 或回复触发。"""
        return {name.strip() for name in values if name.strip()}

    @field_validator("session_expire_timeout", mode="before")
    @classmethod
    def parse_session_timeout_seconds(cls, value: object) -> object:
        """环境文件中的数值按秒解析，同时保留标准时长字符串支持。"""
        if isinstance(value, str) and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value.strip()):
            return float(value)
        return value

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
    "app_enabled_plugins": "启用的内置插件 (重启生效)",
    # AI 服务
    "chat_python_agent_code": "Python Agent 预设代码 (空值使用当前聊天模型与提示词)",
    "chat_python_search_mcp_code": "Python 网页搜索 MCP 服务代码 (search 分类)",
    "chat_model": "聊天模型名称",
    "chat_vision_model": "图片理解模型名称",
    "chat_image_model": "图片生成模型名称",
    "chat_system_prompt": "系统提示词(可留空)",
    "chat_llm_max_tokens": "LLM 最大生成 Token 数",
    "chat_llm_temperature": "LLM Temperature 采样参数",
    "chat_llm_top_p": "LLM Top P 采样参数",
    "chat_llm_top_k": "LLM Top K 采样参数",
    "chat_llm_min_p": "LLM Min P 采样参数",
    "chat_llm_presence_penalty": "LLM 存在惩罚",
    "chat_llm_frequency_penalty": "LLM 频率惩罚",
    "chat_llm_repetition_penalty": "LLM 重复惩罚",
    "chat_llm_stop": "LLM 停止序列",
    "chat_llm_enable_thinking": "是否启用模型深度思考",
    "chat_llm_forward_user_name": "是否额外发送当前 QQ 昵称元数据",
    "chat_agent_max_steps": "普通聊天 Agent 最大工具推理轮次 (1-5，空值沿用原预算)",
    "chat_mcp_services": "普通聊天可使用的通用 MCP 服务 code 列表",
    "chat_web_search_enabled": "是否为普通 QQ 对话启用受限搜索和网页读取",
    "chat_web_search_max_steps": "联网搜索 Agent 最大推理步数",
    "chat_image_recognition_enabled": "是否识别 QQ 消息中的图片素材",
    "chat_max_images_per_message": "单条消息最多识别的图片数量",
    "chat_image_generation_enabled": "图片生成开关，本地工作流启用前也需开启",
    "chat_image_timeout_seconds": "图片生成请求超时秒数",
    "chat_image_response_format": "图片生成输出格式",
    "chat_image_max_bytes": "单张生成图片最大字节数",
    "chat_local_workflow_enabled": "是否使用本地 Comfy 工作流替代云端生图",
    "chat_local_workflow_base_url": "本地 ComfyUI 服务根地址",
    "chat_local_workflow_directory": "启动期工作流 JSON 导入目录",
    "chat_local_workflow_default_key": "/画图命令使用的默认工作流键",
    "chat_local_workflow_timeout_seconds": "本地 Comfy 工作流总超时秒数",
    "chat_keyword_at_replies": "群聊关键词到 @ 回复文本的映射",
    "chat_auto_mention_enabled": "是否自动识别群聊前 20 字内明确要求代发的 @ 人指令",
    "chat_mention_tool_enabled": "是否允许 Agent 调用群成员 @ 工具",
    "chat_tts_enabled": "是否授权 LLM 自主生成语音回复",
    "chat_tts_url": "GPT-SoVITS /tts 完整接口地址",
    "chat_tts_token": "已废弃，运行时不使用：旧 TTS 服务鉴权令牌",
    "chat_tts_model": "原生音频生成模型名称",
    "chat_tts_voice": "原生音频输出音色",
    "chat_tts_response_format": "原生音频输出格式",
    "chat_tts_speed": "GPT-SoVITS 固定语速倍率",
    "chat_tts_timeout_seconds": "原生音频请求超时秒数",
    "chat_tts_max_input_chars": "单次原生音频请求最大输入字符数",
    "chat_tts_max_audio_bytes": "单次原生音频最大字节数",
    "chat_tts_reference_audio_path": "GPT-SoVITS 固定参考音频路径",
    "chat_tts_reference_text": "GPT-SoVITS 固定参考音频文本",
    "chat_tts_reference_language": "GPT-SoVITS 参考文本语言",
    "chat_tts_text_language": "GPT-SoVITS 目标文本语言",
    "chat_tts_output_directory": "GPT-SoVITS 生成音频目录",
    "chat_mcp_enabled": "是否启用 Chat MCP 工具服务",
    "chat_mcp_mount_path": "Chat MCP 服务挂载路径",
    "chat_mcp_allowed_hosts": "Chat MCP Host 请求白名单",
    "chat_mcp_session_ttl_seconds": "Chat MCP 请求上下文有效秒数",
    "chat_mcp_max_sessions": "Chat MCP 最大并发上下文数",
    "chat_session_secret": "会话密钥 (HMAC 生成会话 ID)",
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
    "nickname": "机器人名称与触发别名",
    "command_start": "命令前缀",
    "command_sep": "命令分隔符",
    "session_expire_timeout": "会话过期时间 (秒)",
    # 聊天行为
    "chat_timeout_seconds": "AI 调用超时 (秒)",
    "chat_max_input_chars": "单条消息最大输入字数",
    "chat_history_turns": "历史对话保留轮数",
    "chat_history_max_chars": "历史对话最大字符数",
    "chat_history_ttl_seconds": "历史对话存活时间 (秒)",
    "chat_group_recent_context_enabled": "是否读取当前群最近普通文本",
    "chat_group_recent_context_messages": "当前群最近文本消息条数",
    "chat_group_recent_context_max_chars": "当前群最近文本最大字符数",
    "chat_group_recent_context_ttl_seconds": "当前群最近文本存活时间 (秒)",
    "chat_memory_enabled": "是否启用会话心智记忆与自动压缩",
    "chat_tagged_memory_enabled": "是否启用由 LLM 自主存取的标签记忆工具",
    "chat_memory_max_chars": "单会话心智记忆最大字符数",
    "chat_memory_compression_max_tokens": "记忆压缩最大生成 Token 数",
    "chat_memory_compression_queue_capacity": "后台记忆压缩最多等待会话数",
    "chat_db_pool_size": "聊天数据库共享连接池容量 (重启生效)",
    "chat_db_timeout_seconds": "聊天数据库连接等待与读写超时秒数 (重启生效)",
    "chat_max_sessions": "最大并发会话数",
    "chat_max_concurrent_requests": "最大并发 AI 请求数 (满额立即拒绝)",
    "chat_cooldown_seconds": "非排队聊天冷却时间 (秒)",
    "chat_chunk_chars": "回复分段字符数",
    "chat_max_reply_chars": "单次回复最大字符数",
    "chat_queue_max_pending": "Redis 对话队列最大待处理任务数",
    "chat_queue_workers": "对话队列文本并发工作数 (重启生效，满额立即拒绝)",
    "chat_queue_media_workers": "Redis 对话队列媒体并发工作数",
    "chat_queue_max_attempts": "对话任务最大尝试次数",
    "chat_queue_retry_initial_seconds": "对话任务首次重试等待秒数",
    "chat_queue_retry_max_seconds": "对话任务最大重试等待秒数",
    "chat_queue_job_ttl_seconds": "对话任务最长存活秒数",
    "chat_queue_result_ttl_seconds": "已生成待发送结果存活秒数",
    # 权限
    "chat_allowed_groups": "私聊白名单群列表",
    "chat_allowed_users": "私聊白名单用户列表",
    "chat_blocked_groups": "群聊黑名单列表",
    "chat_blocked_users": "用户黑名单列表",
    "chat_allow_all": "是否允许所有私聊",
    "chat_group_shared_session": "群聊是否共享会话",
    "chat_priority": "聊天命令优先级",
    "chat_reset_priority": "重置命令优先级",
    # 消息限制
    "chat_user_message_limit": "用户消息条数限制 (0=不限制)",
    "chat_user_message_window_seconds": "消息限制时间窗口 (秒)",
    "chat_config_refresh_interval_seconds": "配置自动刷新间隔 (秒)",
    # 永劫无间
    "chat_naraka_cookie": "永劫无间 Heybox Cookie",
    "chat_naraka_heybox_id": "永劫无间 Heybox 账号 ID",
    "chat_naraka_default_role_id": "永劫无间默认角色 ID",
    "chat_naraka_server": "永劫无间服务器",
    "chat_naraka_season": "旧版兼容；当前赛季从响应读取",
    "chat_naraka_battle_tid": "旧版兼容；当前战斗类型从响应读取",
    "chat_naraka_timeout_seconds": "永劫查询超时 (秒)",
    "chat_naraka_cooldown_seconds": "永劫查询冷却时间 (秒)",
    "chat_naraka_max_concurrent_requests": "永劫查询最大并发数",
    # 自动发言
    "chat_job_enabled": "启用 Python 定时任务、结果轮询和投递确认",
    "chat_job_poll_seconds": "任务执行检查和结果轮询间隔秒数，范围 1-60",
    "chat_auto_speak_enabled": "是否启用自动发言",
    "chat_auto_speak_mcp_code": "已登记的自动发言决策 MCP 服务 code (仅调用 laya_predict)",
    "chat_auto_speak_bot_id": "自动发言机器人 QQ 号",
    "chat_auto_speak_groups": "自动发言目标群列表",
    "chat_auto_speak_trigger_mode": "自动发言触发方式：interval 定时或 message 群消息",
    "chat_auto_speak_prompt": "自动发言生成提示词",
    "chat_auto_speak_laya_prompt": "自动发言决策指令",
    "chat_auto_speak_initial_delay_seconds": "自动发言启动等待秒数",
    "chat_auto_speak_interval_seconds": "自动发言轮次间隔秒数，定时模式至少 60 秒",
    "chat_auto_speak_max_reply_chars": "自动发言最大回复字数",
}
