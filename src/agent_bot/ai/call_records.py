"""普通与 Agent 调用统一归档，保留来源身份并清理结构化上下文凭据。"""

import base64
import hashlib
import json
import re
from datetime import datetime
from typing import Any, Literal

from agent_bot.ai.contracts import AiModel, Page
from agent_bot.ai.database import AiDatabase, page_limit, query, sql_columns
from agent_bot.ai.errors import AiError

CallType = Literal["ordinary", "agent", "auto_speak", "unknown"]
_TABLE = "agent_bot_call_record"
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
    return result


def python_record(row, type, context=None):
    """Python 原字段 content 是回复，在统一表中明确转换为 replay_context。"""
    data = {
        key: json_value(row[key]) if key in {"usage", "revisions"} else row[key]
        for key in ("usage", "revisions", "configured_model", "backend", "entry", "error_code")
        if key in row
    }
    return bounded_record(
        {
            "session_Id": row["session_id"],
            "content": row["prompt"],
            "replay_context": row["content"],
            "create_time": row["created_at"],
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
        }
    )


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
    """列表不携带正文、凭据摘要或大 JSON。"""

    id: int
    session_Id: str | None
    create_time: datetime | None
    duration: int | None
    model: str | None
    request_id: str | None
    type: CallType
    source: str
    source_id: str
    execution_id: str | None
    status: str


class CallDetail(CallSummary):
    """认证管理端查看完整请求、响应及来源身份。"""

    content: str | None
    replay_context: str | None
    user_certificate: str | None
    data: Any = None
    request_context: Any = None


class CallRecordStore(AiDatabase):
    """统一记录分页和幂等历史导入，使用自增主键避免空时间破坏分页。"""

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
        """详情仅按目标主键读取，旧 ID 保存在来源字段。"""
        row = await self.run(
            lambda c: query(c, f"SELECT * FROM {_TABLE} WHERE id=%s", (id,), one=True)
        )
        if row is None:
            raise AiError("AI_RECORD_NOT_FOUND", status_code=404)
        for key in ("data", "request_context"):
            row[key] = json_value(row[key])
        return CallDetail.model_validate(row)

    async def list_calls(self, *, type=None, session_id=None, status=None, limit=50, cursor=None):
        """游标绑定筛选条件，混合来源或缺失时间仍能稳定遍历。"""
        page_limit(limit)
        if type not in {None, "ordinary", "agent", "auto_speak", "unknown"}:
            raise AiError("AI_INVALID_PAGE", status_code=422)
        filters = [type, session_id, status]
        conditions, args = [], []
        for field, value in zip(("type", "session_Id", "status"), filters, strict=True):
            if value is not None:
                conditions.append(f"`{field}`=%s")
                args.append(value)
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError
                obj = json.loads(base64.urlsafe_b64decode(cursor))
                if obj["filters"] != filters or type_of_id(obj["id"]) is False:
                    raise ValueError
                conditions.append("id<%s")
                args.append(obj["id"])
            except (ValueError, KeyError, TypeError) as error:
                raise AiError("AI_INVALID_PAGE", status_code=422) from error
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = await self.run(
            lambda c: query(
                c,
                f"SELECT {sql_columns(CallSummary.model_fields)} FROM {_TABLE}{where} "
                "ORDER BY id DESC LIMIT %s",
                (*args, limit + 1),
            )
        )
        items = [CallSummary.model_validate(row) for row in rows[:limit]]
        after = (
            base64.urlsafe_b64encode(
                json.dumps({"id": items[-1].id, "filters": filters}).encode()
            ).decode()
            if len(rows) > limit
            else None
        )
        return Page[CallSummary](items=items, next_cursor=after)


def type_of_id(value):
    """严格区分布尔值与正整数游标。"""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0
