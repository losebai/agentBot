"""将管理校验错误转换为字段及中文规则，不公开输入值或任意异常原文。"""

from __future__ import annotations

from typing import Any, get_args

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from agent_bot.bootstrap import BootstrapConfig
from agent_bot.config import CONFIG_DESCRIPTIONS, RuntimeConfig
from agent_bot.settings_errors import SettingsValidationError

# 根级组合校验没有 loc，必须按服务端声明的规则定位，不能解析或回显任意异常文本。
_RULES: dict[str, tuple[tuple[str, ...], str]] = {
    "Job requires chat MCP sessions": (("chat_job_enabled", "chat_mcp_enabled"),
                                       "开启后台任务需要同时开启 MCP"),
    "auto-speak interval must be at least 60 seconds": (
        ("chat_auto_speak_interval_seconds",), "定时模式的间隔必须至少为 60 秒"),
    "auto-speak requires bot and groups": (
        ("chat_auto_speak_bot_id", "chat_auto_speak_groups"),
        "启用自动发言必须填写机器人 QQ 和目标群"),
    "Laya auto-speak requires registered MCP code": (
        ("chat_auto_speak_mcp_code",), "启用 Laya 决策必须选择已登记的 MCP 服务"),
    "LLM auto-speak requires an independent decision model": (
        ("chat_auto_speak_decision_model",), "启用 LLM 决策必须选择独立决策模型"),
    "auto-speak requires recent group context": (
        ("chat_group_recent_context_enabled",), "启用自动发言必须开启群近期上下文"),
    "auto-speak requires at least 2 concurrent AI requests": (
        ("chat_max_concurrent_requests",), "启用自动发言时并发请求数必须至少为 2"),
    "chat_chunk_chars cannot exceed max reply chars": (
        ("chat_chunk_chars", "chat_max_reply_chars"), "分段长度不能超过最大回复长度"),
    "reset priority must be lower than chat priority": (
        ("chat_reset_priority", "chat_priority"), "重置优先级数值必须小于聊天优先级数值"),
    "queue retry maximum cannot be less than initial delay": (
        ("chat_queue_retry_max_seconds", "chat_queue_retry_initial_seconds"),
        "最大重试间隔不能小于初始重试间隔"),
    "queue result TTL cannot be less than job TTL": (
        ("chat_queue_result_ttl_seconds", "chat_queue_job_ttl_seconds"),
        "结果保留时间不能小于任务有效期"),
    "OneBot access token and session secret must be different": (
        ("onebot_v11_access_token", "chat_session_secret"), "两种用途的密钥必须不同"),
    "Naraka credentials must be configured together": (
        ("chat_naraka_cookie", "chat_naraka_heybox_id"), "Cookie 和账号 ID 必须同时配置或同时清除"),
    "Naraka cookie is missing required fields": (
        ("chat_naraka_cookie",), "Cookie 缺少必要的认证字段"),
    "local workflow requires image generation to be enabled": (
        ("chat_local_workflow_enabled", "chat_image_generation_enabled"),
        "启用本地工作流必须开启图片生成"),
    "local workflow requires MCP to be enabled": (
        ("chat_local_workflow_enabled", "chat_mcp_enabled"), "启用本地工作流必须开启 MCP"),
    "chat_db_user is required for mysql": (("chat_db_user",), "MySQL 必须配置数据库用户"),
    "chat_db_password is required for mysql": (("chat_db_password",), "MySQL 必须配置数据库密码"),
    "startup settings revision is required": (
        ("startup_revision",), "修改启动配置须携带文件版本，请刷新页面"),
    "configuration update must contain a field": (("values",), "至少修改一个配置字段"),
    "chat input exceeds the context budget or is empty": (
        ("prompt", "messages"), "当前消息不能为空，历史消息总长度不能超过 12000 字符"),
    "invalid agent history": (
        ("prompt", "messages", "attachment_ids"),
        "无附图时消息不能为空；历史不允许系统角色，总长度不能超过 12000 字符"),
    "invalid media conversation": (
        ("model", "prompt", "messages"),
        "模型和消息不能为空；历史不允许系统角色，总长度不能超过 12000 字符"),
    "unexpected audio parameters": (
        ("output_type", "messages", "voice", "audio_format"),
        "图片生成不能携带音频参数或历史消息"),
    "unexpected image parameters": (
        ("output_type", "image_format"), "音频生成不能携带图片格式参数"),
    "duplicate import model": (("models",), "导入列表中的模型别名或上游标识不能重复"),
    "chat_session_secret must contain at least 32 characters": (
        ("chat_session_secret",), "会话密钥必须至少为 32 字符"),
    "onebot_v11_access_token must contain at least 32 characters": (
        ("onebot_v11_access_token",), "OneBot Token 必须至少为 32 字符"),
    "invalid auto-speak bot ID": (("chat_auto_speak_bot_id",), "必须填写有效的机器人 QQ 号"),
    "invalid auto-speak groups": (("chat_auto_speak_groups",), "必须填写 QQ 群号列表"),
    "invalid auto-speak group ID": (("chat_auto_speak_groups",), "必须填写有效的 QQ 群号"),
    "auto-speak supports at most 20 group targets": (
        ("chat_auto_speak_groups",), "目标群最多为 20 个"),
}

_SETTING_RULES = {
    "NapCat startup settings are native only": "该字段只允许通过原生服务菜单修改",
    "startup settings are read only for this source": "当前启动源中的字段只读，无法在管理页面修改",
}
_TYPE_RULES = {
    "missing": "必填字段，不能为空", "extra_forbidden": "不支持该字段",
    "bool_parsing": "必须填写布尔值(开启或关闭)", "bool_type": "必须填写布尔值(开启或关闭)",
    "int_parsing": "必须填写整数", "int_type": "必须填写整数",
    "int_from_float": "必须填写整数", "float_parsing": "必须填写数字",
    "float_type": "必须填写数字", "finite_number": "必须填写有限数字",
    "string_type": "必须填写文本", "string_pattern_mismatch": "格式不符合要求",
    "list_type": "必须填写列表", "set_type": "必须填写列表", "dict_type": "必须填写对象",
    "model_type": "必须填写对象", "literal_error": "不在允许的选项中",
    "enum": "不在允许的选项中", "json_invalid": "JSON 格式无效",
    "url_parsing": "网址格式无效", "url_scheme": "网址协议不符合要求",
}


def _collect_fields(annotation: Any, known: set[str], visited: set[type]) -> None:
    """只从服务端模型收集字段名，额外字段和字典键可能包含秘密，不能直接公开。"""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if annotation in visited:
            return
        visited.add(annotation)
        for name, field in annotation.model_fields.items():
            known.add(name)
            if field.alias:
                known.add(field.alias)
            _collect_fields(field.annotation, known, visited)
    else:
        for argument in get_args(annotation):
            _collect_fields(argument, known, visited)


def _known_fields(request: Request) -> set[str]:
    """覆盖合并后的配置模型、当前请求体和查询参数，不读取实际请求值。"""
    known: set[str] = set()
    visited: set[type] = set()
    for model in (RuntimeConfig, BootstrapConfig):
        _collect_fields(model, known, visited)
    route = request.scope.get("route")
    body = getattr(route, "body_field", None)
    if body is not None:
        _collect_fields(body.field_info.annotation, known, visited)
    dependant = getattr(route, "dependant", None)
    for source in ("query_params", "path_params", "header_params", "cookie_params"):
        for parameter in getattr(dependant, source, ()):
            known.update((parameter.name, parameter.alias))
    return known


def _field_path(location: tuple, known: set[str]) -> str:
    """保留已知嵌套字段和列表索引，未知名称用固定文字替代以免泄露输入。"""
    parts = []
    for part in location:
        if not parts and part in {"body", "query", "path", "header", "cookie"}:
            continue
        if isinstance(part, int):
            if parts:
                parts[-1] += f"[{part}]"
        elif part in known:
            parts.append(part)
        else:
            parts.append("未知字段")
            break
    return ".".join(parts) or "请求内容"


def _rule_message(issue: dict) -> str:
    """内置规则只公开受控边界值；自定义规则的原文和上下文永不直接回显。"""
    kind = issue["type"]
    context = issue.get("ctx", {})
    limits = {
        "greater_than": ("gt", "必须大于"), "greater_than_equal": ("ge", "必须大于或等于"),
        "less_than": ("lt", "必须小于"), "less_than_equal": ("le", "必须小于或等于"),
        "string_too_short": ("min_length", "字符数不能少于"),
        "string_too_long": ("max_length", "字符数不能超过"),
        "too_short": ("min_length", "项目数不能少于"),
        "too_long": ("max_length", "项目数不能超过"),
    }
    if kind in limits:
        key, label = limits[kind]
        limit = context.get(key)
        if isinstance(limit, (int, float)):
            return f"{label} {limit}"
    return _TYPE_RULES.get(kind, "格式或取值不符合要求")


def _label(field: str) -> str:
    """使用配置页面已有中文说明，始终附带可搜索的稳定字段名。"""
    description = CONFIG_DESCRIPTIONS.get(field)
    return f"{description} ({field})" if description else field


def describe_validation_error(
    error: ValidationError | RequestValidationError | SettingsValidationError, request: Request,
) -> str:
    """统一生成安全的逐字段错误；仓库包装的 Pydantic 错误沿显式原因链还原。"""
    known = _known_fields(request)
    if isinstance(error, SettingsValidationError):
        if isinstance(error.__cause__, ValidationError):
            error = error.__cause__
        else:
            fields = tuple(name for name in error.fields if name in known)
            reason = _SETTING_RULES.get(str(error), "配置内容格式或取值不符合要求")
            label = "、".join(map(_label, fields)) or "配置快照"
            return f"配置或输入不符合要求：{label}：{reason}"
    issues = error.errors() if isinstance(error, RequestValidationError) else error.errors(
        include_input=False, include_url=False,
    )
    messages: list[str] = []
    for issue in issues:
        cause = issue.get("ctx", {}).get("error")
        rule = cause.args[0] if isinstance(cause, ValueError) and cause.args else None
        custom = _RULES.get(rule) if isinstance(rule, str) else None
        if issue["type"] == "secret_mask":
            field = issue.get("ctx", {}).get("field")
            label = _label(field) if field in known else "密钥字段"
            reason = "密钥不能填写展示用的星号掩码"
        elif custom is not None:
            fields, reason = custom
            label = "、".join(map(_label, fields)) if fields else _field_path(issue["loc"], known)
        else:
            label = _label(_field_path(issue["loc"], known))
            reason = _rule_message(issue)
        message = f"{label}：{reason}"
        if message not in messages:
            messages.append(message)
    summary = "；".join(messages[:8])
    if len(messages) > 8:
        summary += f"；另有 {len(messages) - 8} 项，请修正后重试"
    return "配置或输入不符合要求：" + summary
