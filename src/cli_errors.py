"""为命令行生成可操作的配置错误，禁止回显输入值和数据库异常原文。"""

import sqlite3

from pydantic import ValidationError
from pymysql.err import DatabaseError

from agent_bot.config import RuntimeConfig
from agent_bot.settings import (
    SettingsAlreadyInitializedError,
    SettingsError,
    SettingsNotInitializedError,
    SettingsSchemaError,
    SettingsValidationError,
)
from agent_bot.static_config import StaticSettingError

_COMBINATION_FIELDS = {
    'auto-speak requires bot, groups and registered MCP code': (
        'CHAT_AUTO_SPEAK_BOT_ID',
        'CHAT_AUTO_SPEAK_GROUPS',
        'CHAT_AUTO_SPEAK_MCP_CODE',
    ),
    "chat_db_user is required for mysql": ("CHAT_DB_USER",),
    "chat_db_password is required for mysql": ("CHAT_DB_PASSWORD",),
}


def describe_config_error(error: Exception | SystemExit) -> str:
    """只使用已知字段、错误类型和驱动编号构造提示，避免密钥进入日志。"""
    if isinstance(error, StaticSettingError):
        return "该字段为静态配置，请修改 .env 或环境变量并重启服务。"
    if isinstance(error, FileExistsError):
        return "目标文件已存在，请选择新文件；原文件未覆盖。"
    if isinstance(error, ValidationError):
        known = set(RuntimeConfig.model_fields) | {
            "chat_db_host", "chat_db_port", "chat_db_user", "chat_db_password", "chat_db_name",
            "chat_db_backend", "chat_db_sqlite_path",
        }
        fields = []
        for issue in error.errors(include_input=False, include_url=False):
            location = issue["loc"]
            name = location[0] if location else None
            if name == "token":
                label = "AGENT_BOT_ADMIN_TOKEN (32-256 位字母、数字、下划线或连字符)"
            elif name in known:
                label = str(name).upper()
            else:
                # 只将已知规则映射为字段名，绝不回显校验上下文或输入值。
                cause = issue.get('ctx', {}).get('error')
                reason = cause.args[0] if isinstance(cause, ValueError) and cause.args else None
                names = _COMBINATION_FIELDS.get(reason, ()) if isinstance(reason, str) else ()
                label = '、'.join(names) if names else "配置组合"
            if label not in fields:
                fields.append(label)
        return "配置校验失败，请检查：" + "、".join(fields) + "；密钥须替换示例占位符。"
    if isinstance(error, SettingsValidationError):
        if isinstance(error.__cause__, ValidationError):
            return "数据库中的运行配置无效。" + describe_config_error(error.__cause__)
        return "数据库中的运行配置格式无效，请检查已保存配置的字段和值类型。"
    if isinstance(error, SettingsAlreadyInitializedError):
        return "配置已导入；请使用 settings list 查看，使用 settings set 修改。"
    if isinstance(error, SettingsNotInitializedError):
        return "尚未初始化运行配置，请执行 agent-bot settings init 或 settings import-env。"
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
