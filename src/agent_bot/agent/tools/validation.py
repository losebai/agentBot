"""将副作用前的参数校验错误转成有界反馈，不复制异常中的原始输入值。"""

import json
from collections.abc import Iterable
from typing import TypedDict

from jsonschema.exceptions import ValidationError as SchemaValidationError
from pydantic import ValidationError as ModelValidationError

from agent_bot.agent.contracts import ToolCall, ToolResult
from agent_bot.agent.errors import AgentError


class ValidationIssue(TypedDict):
    """管理详情和模型共用的字段级说明，不携带错误输入或异常堆栈。"""

    field: str
    rule: str
    message: str


def _field(path: Iterable[str | int]) -> str:
    """只保留有界字段路径，整体业务组合错误用根节点表示。"""
    return ".".join(str(part) for part in path)[:160] or "$"


def _schema_issue(error: SchemaValidationError) -> ValidationIssue:
    """使用校验规则生成说明，禁止直接使用会回显实际参数的异常正文。"""
    path = list(error.absolute_path)
    rule = str(error.validator)
    message = {
        "required": "缺少必填字段",
        "additionalProperties": "包含工具定义之外的字段",
        "enum": "必须使用工具定义允许的取值",
        "const": "必须使用工具定义指定的取值",
        "uniqueItems": "数组不能包含重复项",
        "anyOf": "必须满足工具定义中的一种参数组合",
        "oneOf": "必须且只能满足一种参数组合",
        "not": "不允许使用此参数组合",
    }.get(rule, "参数不符合工具定义")
    bounds = {
        "maximum": "不能大于", "minimum": "不能小于",
        "exclusiveMaximum": "必须小于", "exclusiveMinimum": "必须大于",
        "maxLength": "字符数不能超过", "minLength": "字符数不能少于",
        "maxItems": "项目数不能超过", "minItems": "项目数不能少于",
        "maxProperties": "字段数不能超过", "minProperties": "字段数不能少于",
        "multipleOf": "必须为此值的整数倍：",
    }
    if rule in bounds:
        message = f"{bounds[rule]} {error.validator_value}"
    elif rule == "type":
        message = f"类型必须为 {error.validator_value}"
    elif rule == "required" and isinstance(error.instance, dict):
        path.extend(next(([key] for key in error.validator_value
                          if key not in error.instance), []))
    elif rule == "additionalProperties" and isinstance(error.instance, dict):
        path.extend(next(([key] for key in error.instance
                          if key not in error.schema.get("properties", {})), []))
    return {"field": _field(path), "rule": rule, "message": message[:400]}


def argument_issues(error: AgentError) -> list[ValidationIssue]:
    """沿显式异常原因提取 schema 或本地业务校验，最多公开八项且不保留输入。"""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, SchemaValidationError):
            return [_schema_issue(cause)]
        if isinstance(cause, ModelValidationError):
            return [{"field": _field(item["loc"]), "rule": item["type"],
                     "message": item["msg"][:400]}
                    for item in cause.errors(include_input=False, include_context=False,
                                             include_url=False)[:8]]
        cause = cause.__cause__
    return [{"field": "$", "rule": "arguments", "message": "参数不符合工具定义"}]


def rejected_call(call: ToolCall, error: AgentError | None = None) -> ToolResult:
    """校验失败与同批未执行分别反馈，不把任何拒绝结果描述为执行成功。"""
    code = "AGENT_TOOL_ARGUMENTS_INVALID" if error else "AGENT_TOOL_BATCH_REJECTED"
    message = (
        "参数校验失败，工具未执行。请根据字段错误和工具定义修正后重新调用。"
        if error else "同批其他调用参数校验失败，本工具未执行。需要时在新批次重新调用。"
    )
    return ToolResult(
        name=call.name, call_id=call.call_id, is_error=True,
        content=json.dumps({"error": {"code": code, "message": message,
                                      "issues": argument_issues(error) if error else []}},
                           ensure_ascii=False),
    )
