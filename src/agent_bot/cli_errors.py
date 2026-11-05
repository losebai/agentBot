"""为命令行生成可操作的配置错误，禁止回显输入值和数据库异常原文。"""

import sqlite3

from pydantic import ValidationError
from pymysql.err import DatabaseError

from agent_bot.config import RuntimeConfig
from agent_bot.database_upgrade import UpgradeError
from agent_bot.settings import (
    SettingsAlreadyInitializedError,
    SettingsError,
    SettingsNotInitializedError,
    SettingsSchemaError,
    SettingsValidationError,
)
from agent_bot.static_config import STATIC_SETTING_NAMES, StaticSettingError

_COMBINATION_FIELDS = {
    "local workflow requires MCP to be enabled": (
        "CHAT_LOCAL_WORKFLOW_ENABLED", "CHAT_MCP_ENABLED",
    ),
    "Job requires chat MCP sessions": ("CHAT_JOB_ENABLED", "CHAT_MCP_ENABLED"),
    "OneBot access token and session secret must be different": (
        "ONEBOT_V11_ACCESS_TOKEN", "CHAT_SESSION_SECRET",
    ),
    'auto-speak requires bot, groups and registered MCP code': (
        'CHAT_AUTO_SPEAK_BOT_ID',
        'CHAT_AUTO_SPEAK_GROUPS',
        'CHAT_AUTO_SPEAK_MCP_CODE',
    ),
    "chat_db_user is required for mysql": ("CHAT_DB_USER",),
    "chat_db_password is required for mysql": ("CHAT_DB_PASSWORD",),
}
_COMBINATION_REASONS = {
    "local workflow requires MCP to be enabled": "本地工作流需要开启 MCP",
    "Job requires chat MCP sessions": "后台任务需要开启 MCP",
    "OneBot access token and session secret must be different": "两种用途的密钥必须不同",
}


def describe_config_error(error: Exception | SystemExit) -> str:
    """只使用已知字段、错误类型和驱动编号构造提示，避免密钥进入日志。"""
    if isinstance(error, StaticSettingError):
        return "该字段为静态配置，请修改 .env 或环境变量并重启服务。"
    if isinstance(error, FileExistsError):
        return "目标文件已存在，请选择新文件；原文件未覆盖。"
    if isinstance(error, UpgradeError):
        # 升级器只构造固定提示和发布清单 ID，保留这些可操作的安全错误。
        return str(error)
    if isinstance(error, ValidationError):
        known = set(RuntimeConfig.model_fields) | {
            "chat_db_host", "chat_db_port", "chat_db_user", "chat_db_password", "chat_db_name",
            "chat_db_backend", "chat_db_sqlite_path",
        }
        fields = []
        static_fields: set[str] = set()
        invalid_secret = False
        for issue in error.errors(include_input=False, include_url=False):
            location = issue["loc"]
            name = location[0] if location else None
            if name == "token":
                label = "AGENT_BOT_ADMIN_TOKEN (留空免登录，或 8-256 位字母、数字及符号，不含空格)"
                invalid_secret = True
            elif name in known:
                label = str(name).upper()
                if name in STATIC_SETTING_NAMES:
                    static_fields.add(label)
                if name in {"onebot_v11_access_token", "chat_session_secret"}:
                    invalid_secret = True
            else:
                # 只将已知规则映射为字段名，绝不回显校验上下文或输入值。
                cause = issue.get('ctx', {}).get('error')
                reason = cause.args[0] if isinstance(cause, ValueError) and cause.args else None
                names = _COMBINATION_FIELDS.get(reason, ()) if isinstance(reason, str) else ()
                label = '、'.join(names) if names else "配置组合"
                # 字段与原因均来自固定白名单，未知校验器消息不得写入终端。
                explanation = _COMBINATION_REASONS.get(reason) if isinstance(reason, str) else None
                if explanation:
                    label += f" ({explanation})"
                static_fields.update(
                    field for field in names if field.lower() in STATIC_SETTING_NAMES
                )
            if label not in fields:
                fields.append(label)
        message = "配置校验失败，请检查：" + "、".join(fields) + "。"
        if static_fields:
            message += "静态字段 " + "、".join(sorted(static_fields))
            message += " 请修改 .env 或环境变量并重启。"
        if invalid_secret:
            message += "密钥须满足长度要求并替换示例占位符。"
        return message
    if isinstance(error, SettingsValidationError):
        if isinstance(error.__cause__, ValidationError):
            return (
                "静态配置与数据库运行配置的有效配置组合无效。"
                + describe_config_error(error.__cause__)
            )
        return "数据库中的运行配置格式无效，请检查已保存配置的字段和值类型。"
    if isinstance(error, SettingsAlreadyInitializedError):
        return "配置已导入；请使用 settings list 查看，使用 settings set 修改。"
    if isinstance(error, SettingsNotInitializedError):
        return "尚未初始化运行配置，请执行 taobot settings init 或 settings import-env。"
    if isinstance(error, SettingsSchemaError):
        return "配置表缺失或结构不匹配，请检查所选数据库及初始化脚本；新库执行 database init。"
    if isinstance(error, SettingsError):
        cause = error.__cause__
        if isinstance(cause, sqlite3.Error) or isinstance(
            getattr(cause, "__cause__", None), sqlite3.Error
        ):
            return "SQLite 数据库操作失败，请检查 CHAT_DB_SQLITE_PATH、目录权限和初始化结构。"
        if isinstance(cause, DatabaseError) and cause.args:
            code = cause.args[0]
            if code in {2002, 2003, 2005}:
                return "无法连接 MySQL，请确认服务已启动并检查 CHAT_DB_HOST、CHAT_DB_PORT。"
            if code in {1044, 1045, 1698}:
                return "MySQL 认证或授权失败，请检查 CHAT_DB_USER、CHAT_DB_PASSWORD 和数据库权限。"
            if code == 1049:
                return "MySQL 数据库不存在，请检查 CHAT_DB_NAME 并先创建独立数据库。"
        return "配置数据库操作失败，请检查连接、表结构及运行配置；未输出数据库异常详情。"
    return "配置操作失败，请检查命令参数和 .env 配置。"
