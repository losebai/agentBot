"""有序执行记录、脱敏工具负载及独立附件关联，不保存二进制输出。"""

import base64
import json
import re
from datetime import datetime

from agent_bot.agent.contracts import RunDetail, RunSummary, StepView
from agent_bot.agent.errors import AgentError
from agent_bot.ai.call_records import insert_record, json_value, python_record, sanitize_context
from agent_bot.ai.contracts import CatalogPage
from agent_bot.ai.database import AiDatabase, page_limit, query, sql_columns
from agent_bot.ai.errors import AiError
from agent_bot.media.contracts import MediaOwner, MediaScope


def decode(row):
    """解码 JSON 列，读取时仍脱敏工具负载，兼容历史空值。"""
    return {
        k: sanitize_context(json_value(v)) if k in {"arguments", "result"}
        else json.loads(v) if k in {"usage", "revisions"} and isinstance(v, str) else v
        for k, v in row.items()
    }


class AgentRunStore(AiDatabase):
    """管理执行状态和工具负载，运行详情通过媒体存储查询已发布成果。"""

    def __init__(self, database, *, connect, media_store=None):
        """存储可选，旧文本安装不依赖媒体表。"""
        super().__init__(database, connect=connect)
        self.media_store = media_store

    async def _insert(self, table, record):
        """引用内部列名写入记录，工具负载先脱敏，媒体仅保留公开附件元数据。"""
        values = record.model_dump(exclude={"attachments", "request_context"})
        for field in ("arguments", "result"):
            if field in values:
                values[field] = sanitize_context(values[field])
        for field in ("usage", "revisions", "arguments", "result"):
            if field in values:
                values[field] = (
                    json.dumps(values[field], ensure_ascii=False, allow_nan=False)
                    if values[field] is not None else None
                )

        def write(connection):
            """只持久化记录类型已有的标量及 JSON 列。"""
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {table} ({sql_columns(values)}) VALUES "
                    f"({','.join(['%s'] * len(values))})",
                    tuple(values.values()),
                )
            if table == "agent_bot_agent_run":
                insert_record(connection, python_record(values, "agent", record.request_context))

        await self.run(write, write=True)

    async def begin(self, run):
        await self._insert("agent_bot_agent_run", run)

    async def append_step(self, step):
        await self._insert("agent_bot_agent_step", step)

    async def finish(self, run):
        """原子校验运行中状态并更新终态，usage 列按标识符引用。"""
        values = run.model_dump(exclude={"execution_id"})
        if values["model"] is None:
            values.pop("model")
        values["usage"] = run.usage.model_dump_json()

        def write(connection):
            """加锁后更新终态和用量，防止重复完成覆盖已有结果。"""
            row = query(
                connection,
                "SELECT status FROM agent_bot_agent_run WHERE execution_id=%s FOR UPDATE",
                (run.execution_id,),
                one=True,
            )
            if row is None or row["status"] != "running":
                raise AgentError("AGENT_RECORD_CONFLICT", status_code=409)
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE agent_bot_agent_run SET "
                    + ",".join(f"`{key}`=%s" for key in values)
                    + " WHERE execution_id=%s AND status='running'",
                    (*values.values(), run.execution_id),
                )
            # 原始运行和统一记录在一个事务内完成，不允许只更新其中一份。
            old = query(
                connection,
                "SELECT * FROM agent_bot_agent_run WHERE execution_id=%s",
                (run.execution_id,),
                one=True,
            )
            unified = query(
                connection,
                "SELECT * FROM agent_bot_call_record WHERE source=%s AND source_id=%s",
                ("python:agent_bot_agent_run", run.execution_id),
                one=True,
            )
            if unified is None:
                insert_record(connection, python_record(old, "agent"))
            else:
                updated = python_record(old, "agent")
                with connection.cursor() as cursor:
                    cursor.execute(
                        "UPDATE agent_bot_call_record SET replay_context=%s,model=%s,duration=%s,"
                        "status=%s,data=%s WHERE id=%s",
                        (
                            updated["replay_context"],
                            updated["model"],
                            updated["duration"],
                            updated["status"],
                            json.dumps(updated["data"], ensure_ascii=False),
                            unified["id"],
                        ),
                    )

        await self.run(write, write=True)

    async def get_run(self, execution_id):
        """失败或取消后的已生成附件仍可从管理端运行详情恢复。"""
        if not re.fullmatch(r"[0-9a-f]{32}", execution_id):
            raise AgentError("AGENT_RECORD_NOT_FOUND", status_code=404)
        row = await self.run(
            lambda c: query(
                c,
                "SELECT * FROM agent_bot_agent_run WHERE execution_id=%s",
                (execution_id,),
                one=True,
            )
        )
        if row is None:
            raise AgentError("AGENT_RECORD_NOT_FOUND", status_code=404)
        detail = RunDetail.model_validate(decode(row))
        if self.media_store is not None and detail.entry == "admin":
            try:
                attachments = await self.media_store.list_execution(
                    MediaScope(
                        owner=MediaOwner(entry="admin", session_id=detail.session_id),
                        execution_id=detail.execution_id,
                        execution_kind="agent",
                    )
                )
            except AiError as error:
                if error.code != "AI_NOT_INITIALIZED":
                    raise
                attachments = ()
            detail = detail.model_copy(update={"attachments": attachments})
        return detail

    async def list_runs(
        self, *, limit=10, cursor=None, page=None, session_id=None, status=None
    ):
        """管理页按页码读取运行摘要，兼容调用仍可使用绑定筛选条件的稳定游标。"""
        page_limit(limit)
        if page is not None and (isinstance(page, bool) or page < 1 or cursor is not None):
            raise AgentError("AGENT_INVALID_PAGE", status_code=422)
        conditions, args = [], []
        if session_id is not None:
            if len(session_id) > 256:
                raise AgentError("AGENT_INVALID_PAGE", status_code=422)
            conditions.append("session_id=%s")
            args.append(session_id)
        if status is not None:
            if status not in {"running", "completed", "failed", "cancelled"}:
                raise AgentError("AGENT_INVALID_PAGE", status_code=422)
            conditions.append("status=%s")
            args.append(status)
        if cursor is not None:
            try:
                if len(cursor) > 1024:
                    raise ValueError
                value = json.loads(base64.urlsafe_b64decode(cursor))
                stamp, id = datetime.fromisoformat(value["t"]), value["id"]
                if (
                    not re.fullmatch(r"[0-9a-f]{32}", id)
                    or value["session"] != session_id
                    or value["status"] != status
                ):
                    raise ValueError
            except (ValueError, TypeError, KeyError) as error:
                raise AgentError("AGENT_INVALID_PAGE", status_code=422) from error
            conditions.append("(created_at<%s OR (created_at=%s AND execution_id<%s))")
            args.extend((stamp, stamp, id))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        def read(connection):
            """计数和当前页在同一数据库事务中读取，保证页码与总数口径一致。"""
            total = query(
                connection,
                f"SELECT COUNT(*) AS total FROM agent_bot_agent_run{where}",
                tuple(args),
                one=True,
            )["total"]
            pagination = " LIMIT %s OFFSET %s" if page is not None else " LIMIT %s"
            pagination_args = (limit + 1, (page - 1) * limit) if page is not None else (limit + 1,)
            rows = query(
                connection,
                f"SELECT {sql_columns(RunSummary.model_fields)} FROM agent_bot_agent_run{where} "
                "ORDER BY created_at DESC,execution_id DESC" + pagination,
                (*args, *pagination_args),
            )
            return total, rows

        total, rows = await self.run(read)
        items = [RunSummary.model_validate(decode(row)) for row in rows[:limit]]
        after = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "t": items[-1].created_at.isoformat(),
                        "id": items[-1].execution_id,
                        "session": session_id,
                        "status": status,
                    }
                ).encode()
            ).decode()
            if len(rows) > limit
            else None
        )
        return CatalogPage[RunSummary](items=items, next_cursor=after, total=total)

    async def list_steps(self, execution_id, *, limit=10, cursor=None, page=None):
        """管理页按页码读取步骤，兼容调用仍可使用与执行记录绑定的游标。"""
        page_limit(limit)
        if page is not None and (isinstance(page, bool) or page < 1 or cursor is not None):
            raise AgentError("AGENT_INVALID_PAGE", status_code=422)
        await self.get_run(execution_id)
        after = 0
        if cursor is not None:
            try:
                run, number = cursor.split(":")
                if run != execution_id or not number.isascii() or not number.isdecimal():
                    raise ValueError
                after = int(number)
                if not 0 <= after <= 1000:
                    raise ValueError
            except (ValueError, TypeError) as error:
                raise AgentError("AGENT_INVALID_PAGE", status_code=422) from error
        def read(connection):
            """步骤总数和页内容共用执行标识，避免跨运行记录计数。"""
            total = query(
                connection,
                "SELECT COUNT(*) AS total FROM agent_bot_agent_step WHERE execution_id=%s",
                (execution_id,),
                one=True,
            )["total"]
            if page is not None:
                rows = query(
                    connection,
                    "SELECT * FROM agent_bot_agent_step WHERE execution_id=%s "
                    "ORDER BY sequence LIMIT %s OFFSET %s",
                    (execution_id, limit + 1, (page - 1) * limit),
                )
            else:
                rows = query(
                    connection,
                    "SELECT * FROM agent_bot_agent_step "
                    "WHERE execution_id=%s AND sequence>%s ORDER BY sequence LIMIT %s",
                    (execution_id, after, limit + 1),
                )
            return total, rows

        total, rows = await self.run(read)
        items = [StepView.model_validate(decode(row)) for row in rows[:limit]]
        return CatalogPage[StepView](
            items=items,
            next_cursor=f"{execution_id}:{items[-1].sequence}" if len(rows) > limit else None,
            total=total,
        )
