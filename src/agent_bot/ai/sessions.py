"""独立保存 AI 会话记录并跨数据库引用列名，不混入 QQ 记忆或供应商密钥。"""

import base64
import json
from datetime import datetime

from agent_bot.ai.call_records import insert_record, python_record
from agent_bot.ai.contracts import Page, SessionDetail, SessionSummary, SessionWrite
from agent_bot.ai.database import AiDatabase, page_limit, query, sql_columns
from agent_bot.ai.errors import AiError

_SUMMARY = sql_columns(SessionSummary.model_fields)


def decode_row(row: dict) -> dict:
    return {
        **row,
        "usage": json.loads(row["usage"]) if isinstance(row["usage"], str) else row["usage"],
    }


class AiSessionStore(AiDatabase):
    """维护会话详情及不包含原始提示文本的分页摘要。"""

    async def save(self, record: SessionWrite) -> None:
        """参数化保存记录值，并引用 usage 等可能与保留字冲突的列名。"""
        values = record.model_dump(exclude={'request_context'})
        values["usage"] = record.usage.model_dump_json()

        def write(connection):
            """同一事务写入会话正文和 JSON token 用量。"""
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO agent_bot_ai_session ("
                    + sql_columns(values)
                    + ") VALUES ("
                    + ",".join(["%s"] * len(values))
                    + ")",
                    tuple(values.values()),
                )
            insert_record(connection, python_record(values, 'ordinary', record.request_context))

        await self.run(write, write=True)

    async def list_records(
        self,
        *,
        session_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> Page[SessionSummary]:
        page_limit(limit)
        conditions, args = [], []
        if session_id is not None:
            if len(session_id) > 256:
                raise AiError("AI_INVALID_PAGE", status_code=422)
            conditions.append("session_id=%s")
            args.append(session_id)
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
                id = value["id"]
                if not isinstance(id, str) or len(id) != 32:
                    raise ValueError
            except (ValueError, KeyError, TypeError) as error:
                raise AiError("AI_INVALID_PAGE", status_code=422) from error
            conditions.append("(created_at<%s OR (created_at=%s AND execution_id<%s))")
            args.extend([stamp, stamp, id])
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = await self.run(
            lambda connection: query(
                connection,
                f"SELECT {_SUMMARY} FROM agent_bot_ai_session{where} "
                "ORDER BY created_at DESC,execution_id DESC LIMIT %s",
                (*args, limit + 1),
            )
        )
        items = [SessionSummary.model_validate(decode_row(row)) for row in rows[:limit]]
        after = None
        if len(rows) > limit:
            after = base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "t": items[-1].created_at.isoformat(),
                        "id": items[-1].execution_id,
                    }
                ).encode()
            ).decode()
        return Page[SessionSummary](items=items, next_cursor=after)

    async def get_record(self, execution_id: str) -> SessionDetail:
        if len(execution_id) != 32 or any(c not in "0123456789abcdef" for c in execution_id):
            raise AiError("AI_RECORD_NOT_FOUND", status_code=404)
        row = await self.run(
            lambda connection: query(
                connection,
                "SELECT * FROM agent_bot_ai_session WHERE execution_id=%s",
                (execution_id,),
                one=True,
            )
        )
        if row is None:
            raise AiError("AI_RECORD_NOT_FOUND", status_code=404)
        return SessionDetail.model_validate(decode_row(row))
