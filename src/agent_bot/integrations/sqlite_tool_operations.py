"""用 SQLite 原子执行权和类型化结果避免聊天工具重复副作用。"""

from __future__ import annotations

import sqlite3
import time

from agent_bot.integrations.sqlite_queue_database import run_sqlite
from agent_bot.services.tool_operations import (
    DeliveryEffect,
    EffectToolName,
    ImageEffect,
    ImageTaskEffect,
    MentionEffect,
    ReplyEffect,
    SpeechEffect,
    ToolEffect,
    ToolOperation,
    ToolOperationConflict,
    VideoEffect,
)

_RESULT_TYPES = {
    "outgoing_message": (DeliveryEffect,),
    "image_generate": (ImageTaskEffect,),
    "send_reply": (ReplyEffect,),
    "image_start_reply": (ReplyEffect,),
    "image_completion_reply": (ReplyEffect,),
    "mention_member": (MentionEffect,),
    "local_workflow_generate_image": (ImageEffect, ImageTaskEffect),
    "local_workflow_generate_video": (VideoEffect, ImageTaskEffect),
    "tts_generate": (SpeechEffect,),
}


class SqliteToolOperationStore:
    """执行中或结果不确定的操作不得重新取得执行权，结果可跨进程恢复。"""

    def __init__(self, path: str, *, key_prefix: str, ttl_seconds: int) -> None:
        """复用队列文件及保留策略，初始化不创建数据库或额外连接。"""
        if not 1 <= ttl_seconds <= 86_400 or not key_prefix:
            raise ValueError("tool operation retention is invalid")
        self.path = path
        self.prefix = key_prefix
        self.ttl = ttl_seconds

    def _identity(self, job_id: str, tool: EffectToolName) -> tuple[str, str, str]:
        """完整的命名空间、任务和工具共同组成主键，避免分隔符碰撞。"""
        if not job_id or len(job_id) > 128 or tool not in _RESULT_TYPES:
            raise ValueError("tool operation identity is invalid")
        return self.prefix, job_id, tool

    def _validate_result(self, tool: EffectToolName, result: ToolEffect | None) -> None:
        """恢复结果必须属于当前授权工具，媒体类型不能交叉复用。"""
        if not isinstance(result, _RESULT_TYPES[tool]):
            raise ValueError("tool operation result type is invalid")

    def _purge(self, connection: sqlite3.Connection) -> None:
        """保留期必须由装配层覆盖任务寿命，过期后回收历史操作。"""
        connection.execute(
            "DELETE FROM chat_tool_operations WHERE namespace=? AND expires_at<=?",
            (self.prefix, time.time()),
        )

    def _read(
        self, connection: sqlite3.Connection, identity: tuple[str, str, str],
    ) -> ToolOperation | None:
        """在调用者事务中读取尚未过期的执行权或结果。"""
        row = connection.execute(
            "SELECT payload FROM chat_tool_operations "
            "WHERE namespace=? AND job_id=? AND tool=? AND expires_at>?",
            (*identity, time.time()),
        ).fetchone()
        return ToolOperation.model_validate_json(row[0]) if row else None

    async def begin(
        self, job_id: str, tool: EffectToolName, signature: str,
    ) -> ToolEffect | None:
        """串行检查并创建执行权，重复请求只能复用参数一致的完成结果。"""
        identity = self._identity(job_id, tool)
        operation = ToolOperation(signature=signature)

        def begin(connection: sqlite3.Connection) -> ToolEffect | None:
            """BEGIN IMMEDIATE 保证独立实例竞争时只有一个调用获准执行。"""
            self._purge(connection)
            existing = self._read(connection, identity)
            if existing is not None:
                if existing.signature != signature or existing.status != "COMPLETED":
                    raise ToolOperationConflict("工具已执行或结果尚未确认，本次请求不重复执行")
                self._validate_result(tool, existing.result)
                return existing.result
            connection.execute(
                "INSERT INTO chat_tool_operations "
                "(namespace,job_id,tool,payload,expires_at) VALUES (?,?,?,?,?)",
                (*identity, operation.model_dump_json(), time.time() + self.ttl),
            )
            return None

        return await run_sqlite(self.path, begin)

    async def complete(
        self, job_id: str, tool: EffectToolName, signature: str, result: ToolEffect,
    ) -> None:
        """副作用成功后持久化结果，缺失或参数变化的执行权保持失败。"""
        identity = self._identity(job_id, tool)
        self._validate_result(tool, result)
        operation = ToolOperation(signature=signature, status="COMPLETED", result=result)

        def complete(connection: sqlite3.Connection) -> None:
            """检查和更新同属一个写事务，其他竞争者不能替换执行权。"""
            self._purge(connection)
            existing = self._read(connection, identity)
            if existing is None and tool == "outgoing_message":
                # 出站日志已独占执行权；平台确认后直接保存回执，不建立第二个待发送占位。
                connection.execute(
                    "INSERT INTO chat_tool_operations "
                    "(namespace,job_id,tool,payload,expires_at) VALUES (?,?,?,?,?)",
                    (*identity, operation.model_dump_json(), time.time() + self.ttl),
                )
                return
            if existing is None or existing.signature != signature:
                raise ToolOperationConflict("工具执行状态不可用")
            connection.execute(
                "UPDATE chat_tool_operations SET payload=?,expires_at=? "
                "WHERE namespace=? AND job_id=? AND tool=?",
                (operation.model_dump_json(), time.time() + self.ttl, *identity),
            )

        await run_sqlite(self.path, complete)

    async def completed(self, job_id: str, tool: EffectToolName) -> ToolEffect | None:
        """恢复已确认的结果；缺失、过期或执行中均不伪造完成状态。"""
        identity = self._identity(job_id, tool)

        def completed(connection: sqlite3.Connection) -> ToolEffect | None:
            """读取后校验结果类型，确保重启后仍保持工具权限边界。"""
            self._purge(connection)
            existing = self._read(connection, identity)
            if existing is None or existing.result is None:
                return None
            self._validate_result(tool, existing.result)
            return existing.result

        return await run_sqlite(self.path, completed)
