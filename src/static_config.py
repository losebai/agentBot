"""启动固定字段的独立模型，与数据库运行值共用字段约束和最终组合校验。"""

import json
from copy import deepcopy
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, ValidationInfo, create_model, field_validator

from agent_bot.config import RuntimeConfig
from agent_bot.runtime_fields import RESTART_REQUIRED_FIELDS

STATIC_SETTING_NAMES = RESTART_REQUIRED_FIELDS | frozenset(
    {
        "chat_tts_output_directory",
        "chat_memory_compression_queue_capacity",
    }
)
DYNAMIC_SETTING_NAMES = frozenset(RuntimeConfig.model_fields) - STATIC_SETTING_NAMES


class StaticSettingError(ValueError):
    """静态字段只可在启动文件修改，不能通过运行配置接口写入。"""


class StaticConfigBase(BaseModel):
    """只读取启动字段，动态环境变量不会进入该模型或参与校验。"""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True, str_strip_whitespace=True)

    @field_validator("*", mode="before")
    @classmethod
    def parse_environment_values(cls, value: Any, info: ValidationInfo) -> Any:
        """兼容 NoneBot 的列表、集合和秒数环境格式，秘密空值规则交给完整校验。"""
        if not isinstance(value, str):
            return value
        if not value and type(None) in get_args(cls.model_fields[info.field_name].annotation):
            return None
        if info.field_name in {
            "superusers",
            "nickname",
            "command_start",
            "command_sep",
            "onebot_v11_ws_urls",
            "onebot_v11_api_roots",
            "chat_mcp_allowed_hosts",
        }:
            return json.loads(value)
        if info.field_name == "session_expire_timeout":
            return float(value) if value.replace(".", "", 1).isdigit() else value
        if info.field_name in {"onebot_v11_secret", "chat_local_workflow_directory"} and not value:
            return None
        return value


# 字段定义只有一处来源；最终 RuntimeConfig 仍执行密钥、路径和跨字段校验。
StaticConfig = create_model(
    "StaticConfig",
    __base__=StaticConfigBase,
    **{
        name: (definition.annotation, deepcopy(definition))
        for name, definition in RuntimeConfig.model_fields.items()
        if name in STATIC_SETTING_NAMES
    },
)

# 仅供显式 import-env 的环境源解析字段，组合与业务约束仍由 RuntimeConfig 执行。
DynamicConfigSource = create_model(
    "DynamicConfigSource",
    __config__=ConfigDict(extra="ignore", hide_input_in_errors=True),
    **{
        name: (definition.annotation, deepcopy(definition))
        for name, definition in RuntimeConfig.model_fields.items()
        if name in DYNAMIC_SETTING_NAMES
    },
)


def static_values(config: BaseModel | dict[str, Any]) -> dict[str, Any]:
    """抽取静态字段，禁止把业务动态值作为启动默认值。"""
    values = config.model_dump() if isinstance(config, BaseModel) else config
    return StaticConfig.model_validate(values).model_dump()
