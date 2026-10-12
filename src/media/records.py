"""媒体执行记录的稳定分页与终态保护。"""

import base64
import binascii
import json
import re
from datetime import datetime

from agent_bot.ai.contracts import Page
from agent_bot.ai.database import AiDatabase, page_limit, query
from agent_bot.media.contracts import MediaRunDetail, MediaRunSummary
from agent_bot.media.errors import MediaError


class MediaRunStore(AiDatabase):
    """运行记录可尽力保存，文件元数据独立且不可省略。"""

    async def begin(self, run):
        """开始时保存有限文本，不写入媒体字节或上游凭据。"""
        values = run.model_dump()
        await self.run(
            lambda c: query(
                c,
                "INSERT INTO agent_bot_media_run ("
                + ",".join(values)
                + ") VALUES ("
                + ",".join("%s" for _ in values)
                + ")",
                tuple(values.values()),
            ),
            write=True,
        )

    async def finish(self, run):
        """只有运行中记录可收束，重复或迟到的终态写入返回冲突。"""
        values = run.model_dump(exclude={"execution_id"})

        def write(connection):
            """锁住单行，在同事务核对状态并更新终态。"""
            row = query(
                connection,
                "SELECT status FROM agent_bot_media_run WHERE execution_id=%s FOR UPDATE",
                (run.execution_id,),
                one=True,
            )
            if row is None or row["status"] != "running":
                raise MediaError("MEDIA_RECORD_CONFLICT", status_code=409)
            query(
                connection,
                "UPDATE agent_bot_media_run SET "
                + ",".join(f"{key}=%s" for key in values)
                + " WHERE execution_id=%s AND status='running'",
                (*values.values(), run.execution_id),
            )

        await self.run(write, write=True)

    async def get_run(self, execution_id):
        """查询完整文本记录，附件由已认证 API 按可信运行归属补齐。"""
        if not re.fullmatch(r"[0-9a-f]{32}", execution_id):
            raise MediaError("MEDIA_RECORD_NOT_FOUND", status_code=404)
        row = await self.run(
            lambda c: query(
                c,
                "SELECT * FROM agent_bot_media_run WHERE execution_id=%s",
                (execution_id,),
                one=True,
            )
        )
        if row is None:
            raise MediaError("MEDIA_RECORD_NOT_FOUND", status_code=404)
        return MediaRunDetail.model_validate(row)

    async def list_runs(self, *, limit=50, cursor=None, session_id=None, status=None, entry=None):
        """按时间和 ID 倒序分页，游标不能移用于其他筛选条件。"""
        page_limit(limit)
        conditions, args = [], []
        if entry is not None:
            if entry not in {"admin", "qq"}:
                raise MediaError("MEDIA_INVALID_PAGE", status_code=422)
            conditions.append("entry=%s")
            args.append(entry)
        if session_id is not None:
            if not isinstance(session_id, str) or not 1 <= len(session_id) <= 256:
                raise MediaError("MEDIA_INVALID_PAGE", status_code=422)
            conditions.append("session_id=%s")
            args.append(session_id)
        if status is not None:
            if status not in {"running", "completed", "failed", "cancelled"}:
                raise MediaError("MEDIA_INVALID_PAGE", status_code=422)
            conditions.append("status=%s")
            args.append(status)
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 1024:
                    raise ValueError
                value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                stamp, id = datetime.fromisoformat(value["t"]), value["id"]
                if (
                    not re.fullmatch(r"[0-9a-f]{32}", id)
                    or value["session"] != session_id
                    or value["status"] != status
                    or stamp.tzinfo is not None
                    or value.get("entry") != entry
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError, binascii.Error, AttributeError) as error:
                raise MediaError("MEDIA_INVALID_PAGE", status_code=422) from error
            conditions.append("(created_at<%s OR (created_at=%s AND execution_id<%s))")
            args.extend((stamp, stamp, id))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        rows = await self.run(
            lambda c: query(
                c,
                "SELECT "
                + ",".join(MediaRunSummary.model_fields)
                + f" FROM agent_bot_media_run{where} "
                "ORDER BY created_at DESC,execution_id DESC LIMIT %s",
                (*args, limit + 1),
            )
        )
        items = [MediaRunSummary.model_validate(row) for row in rows[:limit]]
        after = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "t": items[-1].created_at.isoformat(),
                        "id": items[-1].execution_id,
                        "session": session_id,
                        "status": status,
                        "entry": entry,
                    }
                ).encode()
            ).decode()
            if len(rows) > limit
            else None
        )
        return Page[MediaRunSummary](items=items, next_cursor=after)
