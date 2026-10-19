"""普通执行接口复用统一调用归档，保留原始 UTC 精度并恢复兼容扩展中的长字段。"""

import base64
import json
from datetime import datetime
from typing import Any

from agent_bot.ai.call_records import insert_record, json_value, python_record
from agent_bot.ai.contracts import Page, SessionDetail, SessionSummary, SessionWrite
from agent_bot.ai.database import AiDatabase, page_limit, query
from agent_bot.ai.errors import AiError

ORDINARY_SOURCE = "python:agent_bot_ai_session"
_TABLE = "agent_bot_call_record"
# 列表只投影有界元数据，不读取输入、回复或扩展里的超长正文。
_SUMMARY = """
execution_id,session_Id AS session_id,request_id,model,status,duration AS duration_ms,
created_at_utc AS created_at,
JSON_OBJECT(
 'configured_model',COALESCE(JSON_EXTRACT(data,'$.configured_model'),
                            JSON_EXTRACT(data,'$.original_data.configured_model')),
 'usage',COALESCE(JSON_EXTRACT(data,'$.usage'),JSON_EXTRACT(data,'$.original_data.usage')),
 'error_code',COALESCE(JSON_EXTRACT(data,'$.error_code'),
                      JSON_EXTRACT(data,'$.original_data.error_code')),
 'session_id',JSON_EXTRACT(data,'$.full_fields.session_Id'),
 'request_id',JSON_EXTRACT(data,'$.full_fields.request_id'),
 'model',JSON_EXTRACT(data,'$.full_fields.model')
) AS metadata
"""


def session_row(row: dict[str, Any], *, detail: bool = False) -> dict[str, Any]:
    """只还原普通接口字段，统一记录 ID、请求快照和内部兼容数据不泄露到响应。"""
    result = dict(row)
    metadata = json_value(result.pop("metadata"))
    for name in ("session_id", "request_id", "model"):
        if metadata[name] is not None:
            result[name] = metadata[name]
    result.update({name: metadata[name] for name in ("configured_model", "usage", "error_code")})
    if detail:
        data = json_value(result.pop("data"))
        full = data.get("full_fields", {})
        result["prompt"] = full.get("content", result.pop("input_content"))
        result["content"] = full.get("replay_context", result.pop("output_content"))
    return result


class AiSessionStore(AiDatabase):
    """保留普通执行仓储接口，所有持久化只发生在统一调用记录表。"""

    async def save(self, record: SessionWrite) -> None:
        """单事务仅保存一份归档，失败不会留下另一张表中的独立副本。"""
        values = record.model_dump(exclude={"request_context"})
        await self.run(
            lambda connection: insert_record(
                connection, python_record(values, "ordinary", record.request_context),
            ), write=True,
        )

    async def list_records(
        self, *, session_id: str | None = None, status: str | None = None,
        limit: int = 50, cursor: str | None = None,
    ) -> Page[SessionSummary]:
        """按原始 UTC 微秒和执行 ID 翻页，只查询本地普通调用来源。"""
        page_limit(limit)
        conditions, args = ["source=%s"], [ORDINARY_SOURCE]
        if session_id is not None:
            if len(session_id) > 256:
                raise AiError("AI_INVALID_PAGE", status_code=422)
            conditions.append("session_Id=%s")
            args.append(session_id[:255])
            if len(session_id) > 255:
                conditions.append(
                    "JSON_EXTRACT(data,'$.full_fields.session_Id')=JSON_EXTRACT(%s,'$')",
                )
                args.append(json.dumps(session_id))
            else:
                conditions.append("JSON_EXTRACT(data,'$.full_fields.session_Id') IS NULL")
        if status is not None:
            if status not in {"completed", "failed", "cancelled"}:
                raise AiError("AI_INVALID_PAGE", status_code=422)
            conditions.append("status=%s")
            args.append(status)
        if cursor is not None:
            try:
                if len(cursor) > 512:
                    raise ValueError
                value = json.loads(base64.urlsafe_b64decode(cursor).decode())
                stamp = datetime.fromisoformat(value["t"])
                execution_id = value["id"]
                if not isinstance(execution_id, str) or len(execution_id) != 32:
                    raise ValueError
            except (ValueError, KeyError, TypeError) as error:
                raise AiError("AI_INVALID_PAGE", status_code=422) from error
            conditions.append(
                "(created_at_utc<%s OR (created_at_utc=%s AND execution_id<%s))",
            )
            args.extend([stamp, stamp, execution_id])
        rows = await self.run(lambda connection: query(
            connection, f"SELECT {_SUMMARY} FROM {_TABLE} WHERE " + " AND ".join(conditions)
            + " ORDER BY created_at_utc DESC,execution_id DESC LIMIT %s",
            (*args, limit + 1),
        ))
        items = [SessionSummary.model_validate(session_row(row)) for row in rows[:limit]]
        after = None
        if len(rows) > limit:
            after = base64.urlsafe_b64encode(json.dumps({
                "t": items[-1].created_at.isoformat(), "id": items[-1].execution_id,
            }).encode()).decode()
        return Page[SessionSummary](items=items, next_cursor=after)

    async def get_record(self, execution_id: str) -> SessionDetail:
        """按稳定来源加执行 ID 查详情，完整恢复超过旧 TEXT 容量的输入输出。"""
        if len(execution_id) != 32 or any(c not in "0123456789abcdef" for c in execution_id):
            raise AiError("AI_RECORD_NOT_FOUND", status_code=404)
        row = await self.run(lambda connection: query(
            connection,
            f"SELECT {_SUMMARY},data,content AS input_content,replay_context AS output_content "
            f"FROM {_TABLE} WHERE source=%s AND source_id=%s",
            (ORDINARY_SOURCE, execution_id), one=True,
        ))
        if row is None:
            raise AiError("AI_RECORD_NOT_FOUND", status_code=404)
        return SessionDetail.model_validate(session_row(row, detail=True))
