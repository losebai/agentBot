"""媒体执行记录的稳定分页与终态保护。"""

import base64
import binascii
import json
import re
from datetime import datetime

from agent_bot.ai.contracts import CatalogPage
from agent_bot.ai.database import AiDatabase, page_limit, query
from agent_bot.media.contracts import MediaRunDetail, MediaRunSummary
from agent_bot.media.errors import MediaError


class MediaRunStore(AiDatabase):
    """运行记录可尽力保存，文件元数据独立且不可省略。"""

    @staticmethod
    def _add_model_display_names(connection, rows):
        """按稳定引用或唯一旧别名补齐供应商名称，失效身份保留记录原值。"""
        references = {row["model"] for row in rows}
        ids = {
            int(reference.removeprefix("model:"))
            for reference in references
            if re.fullmatch(r"model:[1-9][0-9]*", reference)
        }
        conditions, args = [], []
        if ids:
            conditions.append("m.id IN (" + ",".join("%s" for _ in ids) + ")")
            args.extend(ids)
        if references:
            placeholders = ",".join("%s" for _ in references)
            conditions.append(
                f"(m.alias IN ({placeholders}) OR m.upstream_model IN ({placeholders}))"
            )
            args.extend(references)
            args.extend(references)
        models = query(
            connection,
            "SELECT m.id,m.alias,m.upstream_model,p.name AS provider_name "
            "FROM agent_bot_ai_model m JOIN agent_bot_ai_provider p ON p.id=m.provider_id WHERE "
            + " OR ".join(conditions),
            tuple(args),
        ) if conditions else []
        for row in rows:
            candidates = [
                model for model in models
                if row["model"] in {
                    f'model:{model["id"]}', model["alias"], model["upstream_model"]
                }
            ]
            row["model_display_name"] = (
                f'{candidates[0]["provider_name"]} / {candidates[0]["upstream_model"]}'
                if len(candidates) == 1
                else row["model"]
            )
        return rows

    async def begin(self, run):
        """开始时保存有限文本，不写入媒体字节或上游凭据。"""
        values = run.model_dump(exclude={"model_display_name"})
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

        def read(connection):
            """不存在的记录保持空值，避免展示名补全掩盖标准 404。"""
            row = query(
                connection,
                "SELECT * FROM agent_bot_media_run WHERE execution_id=%s",
                (execution_id,),
                one=True,
            )
            return self._add_model_display_names(connection, [row])[0] if row else None

        row = await self.run(read)
        if row is None:
            raise MediaError("MEDIA_RECORD_NOT_FOUND", status_code=404)
        return MediaRunDetail.model_validate(row)

    async def list_runs(
        self, *, limit=10, cursor=None, page=None, session_id=None, status=None, entry=None
    ):
        """管理页按页码倒序读取，兼容调用的游标仍与全部筛选条件绑定。"""
        page_limit(limit)
        if page is not None and (isinstance(page, bool) or page < 1 or cursor is not None):
            raise MediaError("MEDIA_INVALID_PAGE", status_code=422)
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
        stored_fields = tuple(
            field for field in MediaRunSummary.model_fields if field != "model_display_name"
        )
        def read(connection):
            """在一次数据库事务内读取筛选总数和页内容。"""
            total = query(
                connection,
                f"SELECT COUNT(*) AS total FROM agent_bot_media_run{where}",
                tuple(args),
                one=True,
            )["total"]
            pagination = " LIMIT %s OFFSET %s" if page is not None else " LIMIT %s"
            pagination_args = (limit + 1, (page - 1) * limit) if page is not None else (limit + 1,)
            rows = query(
                connection,
                "SELECT "
                + ",".join(stored_fields)
                + f" FROM agent_bot_media_run{where} "
                + "ORDER BY created_at DESC,execution_id DESC"
                + pagination,
                (*args, *pagination_args),
            )
            return total, self._add_model_display_names(connection, rows)

        total, rows = await self.run(read)
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
        return CatalogPage[MediaRunSummary](items=items, next_cursor=after, total=total)
