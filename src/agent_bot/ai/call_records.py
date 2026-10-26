"""普通与 Agent 调用统一归档，保留来源身份并清理结构化上下文凭据。"""

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import Field

from agent_bot.agent.contracts import StepView
from agent_bot.ai.call_origin import auto_speak_call, command_call
from agent_bot.ai.contracts import AiModel, Page, TokenUsage
from agent_bot.ai.database import AiDatabase, page_limit, query, sql_columns
from agent_bot.ai.errors import AiError

CallType = Literal["ordinary", "agent", "auto_speak", "unknown"]
CallCategory = Literal["conversation", "system"]
SystemKind = Literal["auto_speak", "command"]
_TABLE = "agent_bot_call_record"
_RECORD_TIMEZONE = timezone(timedelta(hours=8))
# 查询兼容已入库但类型未重新归类的决策，不要求重写历史数据。
_DECISION_CONDITION = """(
    type = 'auto_speak'
    OR source = 'python:agent_bot_auto_speak_decision'
    OR SUBSTR(request_id, 1, 14) = 'auto-decision-'
    OR JSON_EXTRACT(data, '$.type') IN ('auto_speak_decision', '"auto_speak_decision"')
    OR JSON_EXTRACT(data, '$.system_kind') IN ('auto_speak', '"auto_speak"')
    OR (JSON_EXTRACT(request_context, '$.state') IS NOT NULL
        AND JSON_EXTRACT(data, '$.shouldSpeak') IS NOT NULL)
)"""
# 新指令使用任务标记；历史记录依据已有请求前缀、指令输入及超长归档的原数据识别。
_COMMAND_CONDITION = """(
    COALESCE(JSON_EXTRACT(data, '$.system_kind') IN ('command', '"command"'), 0)
    OR JSON_EXTRACT(data, '$.original_data.system_kind') IN ('command', '"command"')
    OR SUBSTR(request_id, 1, 11) = 'qq-summary-'
    OR SUBSTR(request_id, 1, 8) = 'qq-draw-'
    OR SUBSTR(session_Id, -15) = ':quoted-summary'
    OR (SUBSTR(request_id, 1, 9) = 'qq-reply-' AND SUBSTR(content, 1, 1) = '/')
)"""
_SYSTEM_KIND = (
    f"CASE WHEN {_DECISION_CONDITION} THEN 'auto_speak' "
    f"WHEN {_COMMAND_CONDITION} THEN 'command' ELSE NULL END"
)
_SECRET = re.compile(
    r"^(?:authorization|proxyauthorization|cookie|setcookie|password|passwd|secret|"
    r"apikey|accesskey|accesskeyid|accesskeysecret|credential|credentials|"
    r"token|accesstoken|refreshtoken|sessiontoken|toolsessiontoken|chattoken|"
    r"usercertificate|clientsecret|signature|signedurl|imageurls|videourls)$",
    re.I,
)


def sanitize_context(value: Any) -> Any:
    """递归清除凭据字段，工具参数中的 JSON 字符串也按结构清理。"""
    if isinstance(value, dict):
        return {
            k: sanitize_context(v)
            for k, v in value.items()
            if not _SECRET.fullmatch(re.sub(r"[^a-zA-Z]", "", k))
        }
    if isinstance(value, (list, tuple)):
        return [sanitize_context(v) for v in value]
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(sanitize_context(json.loads(value)), ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    return value


def json_value(value):
    """数据库 JSON 与导出 JSON 共享解析，不把非法 JSON 静默当作空值。"""
    return json.loads(value) if isinstance(value, str) else value


def bounded_record(record):
    """满足旧 TEXT/VARCHAR 上限，超长原文放入 data 扩展字段，不丢失正文。"""
    result = dict(record)
    overflow = {}
    for key in ("content", "replay_context"):
        value = result.get(key)
        if value is not None and len(value.encode("utf-8")) > 65535:
            overflow[key] = value
            result[key] = value.encode("utf-8")[:65535].decode("utf-8", errors="ignore")
    for key in ("session_Id", "request_id", "model"):
        value = result.get(key)
        if value is not None and len(value) > 255:
            overflow[key] = value
            result[key] = value[:255]
    if overflow:
        result["data"] = {"original_data": result.get("data"), "full_fields": overflow}
        if isinstance(record.get("data"), dict) and "system_kind" in record["data"]:
            result["data"]["system_kind"] = record["data"]["system_kind"]
    return result


def call_record_time(value: datetime | str) -> datetime:
    """将原始执行表的 UTC 时间转换为北京时间，与旧记录统一排序和显示。

    原始表继续保存 UTC；统一表的无时区时间约定为北京时间，精度固定到秒。
    显式使用 UTC+8，避免依赖部署机器或数据库会话的本地时区。
    """
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(_RECORD_TIMEZONE).replace(tzinfo=None, microsecond=0)


def source_record_time(value: datetime | str) -> datetime:
    """统一为无时区 UTC 并保留微秒，供普通执行接口和原始时间排序使用。"""
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.astimezone(timezone.utc).replace(tzinfo=None)


def python_record(row, type, context=None):
    """归档输入输出及分类，普通调用同时保留取代旧执行表的原始 UTC 微秒。"""
    data = {
        key: json_value(row[key]) if key in {"usage", "revisions"} else row[key]
        for key in ("usage", "revisions", "configured_model", "backend", "entry", "error_code")
        if key in row
    }
    is_decision = auto_speak_call.get()
    if is_decision:
        # 单次群聊决策沿用普通调用归档，系统分类不混入普通对话分页。
        type = "auto_speak"
        data.update(system_kind="auto_speak", decision_backend="llm")
    elif command_call.get():
        data["system_kind"] = "command"
    return bounded_record(
        {
            "session_Id": row["session_id"],
            "content": row["prompt"],
            "replay_context": row["content"],
            "create_time": call_record_time(row["created_at"]),
            "duration": row["duration_ms"],
            "user_certificate": None,
            "model": row["model"],
            "data": data,
            "request_context": sanitize_context(context),
            "request_id": row["request_id"],
            "type": type,
            "source": "python:agent_bot_agent_run"
            if type == "agent"
            else "python:agent_bot_ai_session",
            "source_id": row["execution_id"],
            "execution_id": row["execution_id"],
            "status": row["status"],
            **({"created_at_utc": source_record_time(row["created_at"])}
               if type in {"ordinary", "auto_speak"} else {}),
        }
    )


def decision_record(row):
    """以北京时间归档决策结果，保留原表 ID，不复制群聊状态或其他输入正文。"""
    result = {
        key: row[key] for key in
        ("mcp_code", "mcp_revision", "should_speak", "confidence", "error_code")
    }
    return bounded_record({
        "session_Id": row["session_id"], "request_id": row["request_id"],
        "content": None, "replay_context": json.dumps(result, ensure_ascii=False),
        "create_time": call_record_time(row["created_at"]), "duration": row["duration_ms"],
        "user_certificate": None, "model": None, "data": result, "request_context": None,
        "type": "auto_speak", "source": "python:agent_bot_auto_speak_decision",
        "source_id": row["id"], "execution_id": None, "status": row["status"],
    })


def legacy_record(row, source):
    """旧系统缺少类型时使用确定证据；无法识别的历史记录保留 unknown。"""
    data, context = json_value(row.get("data")), json_value(row.get("request_context"))
    d, c = data if isinstance(data, dict) else {}, context if isinstance(context, dict) else {}
    if d.get("type") == "auto_speak_decision" or ("state" in c and "shouldSpeak" in d):
        type = "auto_speak"
    elif "toolCalls" in d or c.get("agentId") or "maxSteps" in c:
        type = "agent"
    elif any(k in d for k in ("choices", "output", "stop_reason", "done")):
        type = "ordinary"
    else:
        type = "unknown"
    cert = row.get("user_certificate")
    # 兼容旧库可能保存明文的版本；已有常见摘要保持可关联，明文不可迁入。
    if cert and not re.fullmatch(r"(?:[0-9a-fA-F]{32}|[0-9a-fA-F]{64})", cert):
        cert = "sha256:" + hashlib.sha256(cert.encode()).hexdigest()
    status = d.get("status")
    if status not in {"completed", "failed", "cancelled", "running"}:
        status = "completed" if row.get("replay_context") is not None else "unknown"
    return bounded_record(
        {
            **{
                k: row.get(k)
                for k in (
                    "content",
                    "replay_context",
                    "create_time",
                    "duration",
                    "model",
                    "request_id",
                )
            },
            "session_Id": row.get("session_Id", row.get("session_id")),
            "user_certificate": cert,
            "data": sanitize_context(data),
            "request_context": sanitize_context(context),
            "type": type,
            "source": source,
            "source_id": str(row["id"]),
            "execution_id": None,
            "status": status,
        }
    )


def insert_record(connection, values):
    """在调用者事务内写入统一记录；失败由调用者整体回滚。"""
    values = dict(values)
    for key in ("data", "request_context"):
        values[key] = (
            json.dumps(values[key], ensure_ascii=False, allow_nan=False)
            if values.get(key) is not None
            else None
        )
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {_TABLE} ({sql_columns(values)}) VALUES "
            f"({','.join(['%s'] * len(values))})",
            tuple(values.values()),
        )


class CallSummary(AiModel):
    """列表提供有界用量，不携带正文、凭据摘要或大 JSON。"""

    id: int
    session_Id: str | None
    create_time: datetime | None
    duration: int | None
    model: str | None
    request_id: str | None
    type: CallType
    system_kind: SystemKind | None = None
    source: str
    source_id: str
    execution_id: str | None
    status: str
    usage: TokenUsage = Field(default_factory=TokenUsage)


class CallDetail(CallSummary):
    """认证管理端查看完整请求、响应及来源身份。"""

    content: str | None
    replay_context: str | None
    user_certificate: str | None
    data: Any = None
    request_context: Any = None
    tool_steps: list[StepView] = Field(default_factory=list)


class CallStatistics(AiModel):
    """统一归档的累计用量，缓存比例只使用同时上报缓存和输入用量的调用。"""

    conversation_count: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cached_tokens: int = Field(ge=0)
    cache_prompt_tokens: int = Field(ge=0)
    cache_hit_rate: float | None = Field(default=None, ge=0, le=1)


def _usage_number(*fields: str) -> str:
    """仅解析固定 JSON 数值路径，兼容历史协议及超长归档，不将空值视为零。"""
    paths = [f"$.{prefix}.{field}" for prefix in ("usage", "original_data.usage")
             for field in fields]
    parts = []
    for path in paths:
        value = f"JSON_EXTRACT(data, '{path}')"
        parts.append(
            f"CASE WHEN JSON_TYPE({value}) IN ('INTEGER','DOUBLE','integer','real') "
            f"THEN {value} END"
        )
    return "COALESCE(" + ",".join(parts) + ")" if len(parts) > 1 else parts[0]


def _usage_expressions() -> dict[str, str]:
    """列表、详情和累计统计共用协议映射；总量仅在输入输出均已知时补算。"""
    prompt = _usage_number("prompt_tokens", "input_tokens")
    completion = _usage_number("completion_tokens", "output_tokens")
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": f"COALESCE({_usage_number('total_tokens')},{prompt}+{completion})",
        "cached_tokens": _usage_number(
            "cached_tokens", "prompt_tokens_details.cached_tokens",
            "input_tokens_details.cached_tokens",
        ),
        "cache_prompt_tokens": f"COALESCE({_usage_number('cache_prompt_tokens')},{prompt})",
    }


def _usage_columns() -> str:
    """查询只返回五个用量数值，列表不为显示统计而加载整个归档 JSON。"""
    return ",".join(f"{value} AS usage_{field}"
                    for field, value in _usage_expressions().items())


def _usage_from_row(row: dict[str, Any]) -> TokenUsage:
    """将查询用量列收拢为契约；未上报缓存时不产生虚假的缓存输入分母。"""
    values = {field: row.pop(f"usage_{field}") for field in TokenUsage.model_fields}
    if values["cached_tokens"] is None:
        values["cache_prompt_tokens"] = None
    return TokenUsage.model_validate(values)


class CallRecordStore(AiDatabase):
    """统一记录分页和幂等历史导入，时间相同时由自增主键稳定排序。"""

    async def statistics(self) -> CallStatistics:
        """在数据库内聚合统一记录，避免分页遗漏或重复累加原始执行表。"""
        usage = _usage_expressions()
        prompt, completion = usage["prompt_tokens"], usage["completion_tokens"]
        total, cached = usage["total_tokens"], usage["cached_tokens"]
        cache_prompt = usage["cache_prompt_tokens"]
        row = await self.run(lambda c: query(
            c,
            "SELECT COUNT(CASE WHEN system_kind IS NULL THEN 1 END) AS conversation_count, "
            "COALESCE(SUM(COALESCE(total_tokens,"
            "COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0))),0) "
            "AS total_tokens, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
            "COALESCE(SUM(CASE WHEN cache_prompt_tokens IS NOT NULL THEN cached_tokens END),0) "
            "AS cached_tokens, "
            "COALESCE(SUM(CASE WHEN cached_tokens IS NOT NULL THEN cache_prompt_tokens END),0) "
            "AS cache_prompt_tokens "
            f"FROM (SELECT {_SYSTEM_KIND} AS system_kind, {prompt} AS prompt_tokens, "
            f"{completion} AS completion_tokens, {total} AS total_tokens, "
            f"{cached} AS cached_tokens, {cache_prompt} AS cache_prompt_tokens "
            f"FROM {_TABLE}) AS usage_records",
            one=True,
        ))
        row["cache_hit_rate"] = (
            row["cached_tokens"] / row["cache_prompt_tokens"]
            if row["cache_prompt_tokens"] else None
        )
        return CallStatistics.model_validate(row)

    async def import_batch(self, rows):
        """单批原子导入，已存在的来源主键不会被历史快照覆盖。"""

        def write(connection):
            """唯一约束同时防止不同导入进程重复写入。"""
            pending = []
            known = set()
            for source in {row["source"] for row in rows}:
                ids = [row["source_id"] for row in rows if row["source"] == source]
                found = query(
                    connection,
                    f"SELECT source_id FROM {_TABLE} WHERE source=%s "
                    f"AND source_id IN ({','.join(['%s'] * len(ids))})",
                    (source, *ids),
                )
                known.update((source, row["source_id"]) for row in found)
            for row in rows:
                key = row["source"], row["source_id"]
                if key not in known:
                    pending.append(row)
                    known.add(key)
            if pending:
                columns = list(pending[0])
                params = []
                for row in pending:
                    for field in columns:
                        value = row[field]
                        if field in {"data", "request_context"} and value is not None:
                            value = json.dumps(value, ensure_ascii=False, allow_nan=False)
                        params.append(value)
                clause = "(" + ",".join(["%s"] * len(columns)) + ")"
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"INSERT INTO {_TABLE} ({sql_columns(columns)}) VALUES "
                        + ",".join([clause] * len(pending)),
                        tuple(params),
                    )
            return len(pending)

        return await self.run(write, write=True)

    async def get_call(self, id: int):
        """详情提供用量及脱敏工具负载，内部 UTC 字段不改变已有接口契约。"""
        columns = sql_columns(set(CallDetail.model_fields) - {"tool_steps", "system_kind", "usage"})
        row = await self.run(
            lambda c: query(
                c, f"SELECT {columns}, {_SYSTEM_KIND} AS system_kind, {_usage_columns()} "
                f"FROM {_TABLE} WHERE id=%s",
                (id,), one=True,
            )
        )
        if row is None:
            raise AiError("AI_RECORD_NOT_FOUND", status_code=404)
        row["usage"] = _usage_from_row(row)
        for key in ("data", "request_context"):
            row[key] = json_value(row[key])
        if row["source"] == "python:agent_bot_agent_run" and row.get("execution_id"):
            steps = await self.run(lambda c: query(
                c,
                "SELECT * FROM agent_bot_agent_step WHERE execution_id=%s "
                "AND kind IN ('tool_start','tool_result') ORDER BY sequence",
                (row["execution_id"],),
            ))
            row["tool_steps"] = [StepView.model_validate({
                key: sanitize_context(json_value(value))
                if key in {"arguments", "result"} else value
                for key, value in step.items()
            }) for step in steps]
        return CallDetail.model_validate(row)

    async def list_calls(
        self, *, type=None, session_id=None, status=None, limit=50, cursor=None,
        category=None, system_kind=None,
    ):
        """分页前筛选分组和类型，并只投影用量数值；游标绑定完整条件避免串页。"""
        page_limit(limit)
        if type not in {None, "ordinary", "agent", "auto_speak", "unknown"}:
            raise AiError("AI_INVALID_PAGE", status_code=422)
        if category not in {None, "conversation", "system"} or system_kind not in {
            None, "auto_speak", "command",
        }:
            raise AiError("AI_INVALID_PAGE", status_code=422)
        filters = [type, session_id, status, category, system_kind]
        conditions, args = [], []
        for field, value in zip(("type", "session_Id", "status"), filters[:3], strict=True):
            if value is not None:
                conditions.append(f"`{field}`=%s")
                args.append(value)
        if category is not None:
            null_check = "NOT NULL" if category == "system" else "NULL"
            conditions.append(f"({_SYSTEM_KIND}) IS {null_check}")
        if system_kind is not None:
            conditions.append(f"({_SYSTEM_KIND})=%s")
            args.append(system_kind)
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError
                obj = json.loads(base64.urlsafe_b64decode(cursor))
                if obj["filters"] != filters or type_of_id(obj["id"]) is False:
                    raise ValueError
                stamp = obj["time"]
                if stamp is None:
                    conditions.append("(create_time IS NULL AND id<%s)")
                    args.append(obj["id"])
                else:
                    stamp = datetime.fromisoformat(stamp)
                    conditions.append(
                        "(create_time<%s OR create_time IS NULL OR (create_time=%s AND id<%s))"
                    )
                    args.extend((stamp, stamp, obj["id"]))
            except (ValueError, KeyError, TypeError) as error:
                raise AiError("AI_INVALID_PAGE", status_code=422) from error
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = await self.run(
            lambda c: query(
                c,
                f"SELECT {sql_columns(set(CallSummary.model_fields) - {'system_kind', 'usage'})}, "
                f"{_SYSTEM_KIND} AS system_kind, {_usage_columns()} FROM {_TABLE}{where} "
                "ORDER BY create_time DESC,id DESC LIMIT %s",
                (*args, limit + 1),
            )
        )
        items = []
        for row in rows[:limit]:
            row["usage"] = _usage_from_row(row)
            items.append(CallSummary.model_validate(row))
        after = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "id": items[-1].id,
                        "filters": filters,
                        "time": items[-1].create_time.isoformat()
                        if items[-1].create_time
                        else None,
                    }
                ).encode()
            ).decode()
            if len(rows) > limit
            else None
        )
        return Page[CallSummary](items=items, next_cursor=after)


def type_of_id(value):
    """严格区分布尔值与正整数游标。"""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
