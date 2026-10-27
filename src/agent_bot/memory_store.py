"""MySQL 或 SQLite 的持久聊天记忆，兼容已确认 MySQL 旧表的原位升级。"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import closing, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pymysql
from pymysql.err import DatabaseError, InterfaceError, OperationalError

from agent_bot.database import database_connector
from agent_bot.group_context import RecentGroupMessage, RecentGroupSnapshot
from agent_bot.models import ChatMessage
from agent_bot.settings import DatabaseParams
from agent_bot.state import (
    REQUEST_RECEIPT_TTL_SECONDS,
    ConversationContext,
    ConversationTurn,
    MemoryCompression,
    external_session_id,
    validate_request_id,
)

_SESSION_TABLE = "chat_memory_session"
_TURN_TABLE = "chat_memory_turn"
_RECEIPT_TABLE = "chat_memory_receipt"
_GROUP_CONTEXT_TABLE = "chat_group_context"
_READ_BATCH_SIZE = 128
_MAX_TEXT_BYTES = 1_048_576
_MAX_COMPRESSION_SOURCE_CHARS = 24_000
_MAX_SESSION_ID_LENGTH = 128
_MAX_CLEANUP_ROWS = 500
_MAX_GROUP_MESSAGE_ITEMS = 10_000
_MAX_GROUP_PAYLOAD_BYTES = 1_048_576

_SCHEMA_STATEMENTS = (
    f"""
    CREATE TABLE IF NOT EXISTS `{_SESSION_TABLE}` (
        `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
        `generation_id` CHAR(32) NOT NULL COMMENT '会话代际标识，重建时重新生成',
        `memory_summary` MEDIUMTEXT NOT NULL COMMENT '压缩后的会话摘要',
        `memory_revision` BIGINT NOT NULL DEFAULT 0 COMMENT '摘要乐观锁版本',
        `compacted_through_turn_id` BIGINT NOT NULL DEFAULT 0 COMMENT '已压缩最大轮次 ID',
        `expires_at` DATETIME(6) NOT NULL COMMENT '滑动过期时间',
        `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
        `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
            ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
        PRIMARY KEY (`session_id`),
        KEY `idx_chat_memory_session_expires` (`expires_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='chat 持久会话记忆'
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{_TURN_TABLE}` (
        `id` BIGINT NOT NULL AUTO_INCREMENT COMMENT '轮次主键',
        `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
        `request_id` VARCHAR(128) COLLATE utf8mb4_bin NULL COMMENT '幂等请求标识，旧轮次为空',
        `user_content` MEDIUMTEXT NOT NULL COMMENT '用户历史文本',
        `assistant_content` MEDIUMTEXT NOT NULL COMMENT '已投递助手内容',
        `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
        PRIMARY KEY (`id`),
        KEY `idx_chat_memory_turn_session_id` (`session_id`, `id`),
        UNIQUE KEY `uk_chat_memory_turn_request` (`session_id`, `request_id`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='chat 最近会话轮次'
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{_RECEIPT_TABLE}` (
        `session_id` VARCHAR(128) NOT NULL COMMENT 'HMAC 会话标识',
        `request_id` VARCHAR(128) COLLATE utf8mb4_bin NOT NULL COMMENT '幂等请求标识',
        `expires_at` DATETIME(6) NOT NULL COMMENT '独立重试去重截止时间',
        PRIMARY KEY (`session_id`, `request_id`),
        KEY `idx_chat_memory_receipt_expires` (`expires_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='chat 已提交请求回执'
    """,
    f"""
    CREATE TABLE IF NOT EXISTS `{_GROUP_CONTEXT_TABLE}` (
        `scope_id` VARCHAR(128) NOT NULL COMMENT 'HMAC Bot/群作用域',
        `messages_json` MEDIUMTEXT NOT NULL COMMENT '强类型最近群文本 JSON',
        `expires_at` DATETIME(6) NOT NULL COMMENT '滑动过期时间',
        `create_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
        `update_time` DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
            ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
        PRIMARY KEY (`scope_id`),
        KEY `idx_chat_group_context_expires` (`expires_at`)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='chat 群聊最近文本'
    """,
)

_EXPECTED_SCHEMA = {
    _SESSION_TABLE: {
        "columns": frozenset(
            {
                "session_id",
                "generation_id",
                "memory_summary",
                "memory_revision",
                "compacted_through_turn_id",
                "expires_at",
                "create_time",
                "update_time",
            }
        ),
        "indexes": frozenset({"PRIMARY", "idx_chat_memory_session_expires"}),
    },
    _TURN_TABLE: {
        "columns": frozenset(
            {
                "id",
                "session_id",
                "request_id",
                "user_content",
                "assistant_content",
                "create_time",
            }
        ),
        "indexes": frozenset(
            {"PRIMARY", "idx_chat_memory_turn_session_id", "uk_chat_memory_turn_request"}
        ),
    },
    _RECEIPT_TABLE: {
        "columns": frozenset({"session_id", "request_id", "expires_at"}),
        "indexes": frozenset({"PRIMARY", "idx_chat_memory_receipt_expires"}),
    },
    _GROUP_CONTEXT_TABLE: {
        "columns": frozenset(
            {"scope_id", "messages_json", "expires_at", "create_time", "update_time"}
        ),
        "indexes": frozenset({"PRIMARY", "idx_chat_group_context_expires"}),
    },
}


class ChatMemoryStoreError(RuntimeError):
    """Persistent chat memory operation failed."""


class ChatMemorySchemaError(ChatMemoryStoreError):
    """Persistent memory schema is missing required structures."""


class ChatMemoryValidationError(ValueError):
    """Chat memory input violates a bounded storage contract."""


@dataclass(frozen=True, slots=True)
class ConversationMemoryPolicy:
    """Runtime limits applied to one conversation memory operation."""

    history_turns: int
    max_history_chars: int
    ttl_seconds: float
    memory_enabled: bool
    max_memory_chars: int

    def __post_init__(self) -> None:
        if self.history_turns < 0:
            raise ValueError("history turns cannot be negative")
        if self.max_history_chars <= 0 or self.ttl_seconds <= 0 or self.max_memory_chars <= 0:
            raise ValueError("conversation memory limits must be positive")


@dataclass(frozen=True, slots=True)
class GroupContextPolicy:
    """Runtime limits applied to one group-context operation."""

    message_limit: int
    max_chars: int
    ttl_seconds: float

    def __post_init__(self) -> None:
        if self.message_limit <= 0 or self.max_chars <= 0 or self.ttl_seconds <= 0:
            raise ValueError("group context limits must be positive")


class ChatMemorySchema:
    """创建并严格校验持久记忆表，原位兼容已确认的旧索引名称。"""

    def __init__(
        self,
        params: DatabaseParams,
        *,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        """绑定所选数据库，SQLite 只校验显式初始化后的结构。"""
        self._params = params
        self._connect = connect or database_connector(params)

    def initialize(self) -> None:
        """先迁移旧索引名称再补充字段，避免幂等索引重复创建并保留历史。"""

        try:
            with closing(self._open()) as connection:
                if self._params.backend == "sqlite":
                    # SQLite 结构由显式初始化命令创建，启动仅验证，不执行 MySQL 迁移。
                    self._validate_schema(connection)
                    return
                with connection.cursor() as cursor:
                    for statement in _SCHEMA_STATEMENTS:
                        cursor.execute(statement)
                self._upgrade_index_names(connection)
                self._upgrade_request_ids(connection)
                self._upgrade_generations(connection)
                self._drop_foreign_keys(connection)
                connection.commit()
                self._validate_schema(connection)
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemorySchemaError("chat memory schema initialization failed") from exc

    def validate(self) -> None:
        """普通服务启动只读取结构；缺少字段或索引须通过明确迁移命令修复。"""
        try:
            with closing(self._open()) as connection:
                self._validate_schema(connection)
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemorySchemaError("chat memory schema validation failed") from exc

    def _upgrade_index_names(self, connection: Any) -> None:
        """旧表改名不会同步改索引名，只重命名已确认的 dispatch 历史索引。"""
        for table, expected in _EXPECTED_SCHEMA.items():
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT DISTINCT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS "
                    "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                    (self._params.database, table),
                )
                indexes = {str(row["INDEX_NAME"]) for row in cursor.fetchall()}
                for current in sorted(expected["indexes"] - {"PRIMARY"}):
                    legacy = current.replace("idx_chat_", "idx_dispatch_chat_", 1).replace(
                        "uk_chat_", "uk_dispatch_chat_", 1
                    )
                    if current not in indexes and legacy in indexes:
                        # 标识符只来自固定表结构；不重建索引、不改变唯一性或字段顺序。
                        cursor.execute(
                            f"ALTER TABLE `{table}` RENAME INDEX `{legacy}` TO `{current}`"
                        )

    def _upgrade_request_ids(self, connection: Any) -> None:
        """原位添加可空请求编号与唯一键，保留旧历史数据。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (self._params.database, _TURN_TABLE),
            )
            columns = {str(row["COLUMN_NAME"]).lower() for row in cursor.fetchall()}
            if "request_id" not in columns:
                cursor.execute(
                    f"ALTER TABLE `{_TURN_TABLE}` ADD COLUMN `request_id` "
                    "VARCHAR(128) COLLATE utf8mb4_bin NULL COMMENT '幂等请求标识，旧轮次为空'"
                )
            cursor.execute(
                "SELECT DISTINCT INDEX_NAME FROM INFORMATION_SCHEMA.STATISTICS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (self._params.database, _TURN_TABLE),
            )
            indexes = {str(row["INDEX_NAME"]) for row in cursor.fetchall()}
            if "uk_chat_memory_turn_request" not in indexes:
                cursor.execute(
                    f"ALTER TABLE `{_TURN_TABLE}` ADD UNIQUE KEY "
                    "`uk_chat_memory_turn_request` (`session_id`, `request_id`)"
                )

    def _validate_schema(self, connection: Any) -> None:
        """拒绝缺少必要结构的表，错误只包含安全的结构名称。"""
        for table_name, expected in _EXPECTED_SCHEMA.items():
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COLUMN_NAME
                    FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    """,
                    (self._params.database, table_name),
                )
                columns = {str(row["COLUMN_NAME"]).lower() for row in cursor.fetchall()}
                cursor.execute(
                    """
                    SELECT DISTINCT INDEX_NAME
                    FROM INFORMATION_SCHEMA.STATISTICS
                    WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                    """,
                    (self._params.database, table_name),
                )
                indexes = {str(row["INDEX_NAME"]) for row in cursor.fetchall()}
            missing_columns = sorted(expected["columns"] - columns)
            missing_indexes = sorted(expected["indexes"] - indexes)
            if missing_columns or missing_indexes:
                raise ChatMemorySchemaError(
                    f"chat memory schema is invalid: table={table_name}; "
                    f"missing_columns={','.join(missing_columns) or 'none'}; "
                    f"missing_indexes={','.join(missing_indexes) or 'none'}"
                )

    def _upgrade_generations(self, connection: Any) -> None:
        """为旧会话逐条补充稳定代际，保留历史并支持中断后继续升级。"""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT COLUMN_NAME, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s",
                (self._params.database, _SESSION_TABLE),
            )
            generation = next(
                (row for row in cursor.fetchall() if row["COLUMN_NAME"] == "generation_id"),
                None,
            )
            if generation is None:
                cursor.execute(
                    f"ALTER TABLE `{_SESSION_TABLE}` ADD COLUMN `generation_id` "
                    "CHAR(32) NULL COMMENT '会话代际标识，重建时重新生成'"
                )
            elif generation["IS_NULLABLE"] == "NO":
                return
            cursor.execute(
                f"UPDATE `{_SESSION_TABLE}` SET generation_id = REPLACE(UUID(), '-', '') "
                "WHERE generation_id IS NULL"
            )
            cursor.execute(
                f"ALTER TABLE `{_SESSION_TABLE}` MODIFY COLUMN `generation_id` "
                "CHAR(32) NOT NULL COMMENT '会话代际标识，重建时重新生成'"
            )

    def _drop_foreign_keys(self, connection: Any) -> None:
        """移除旧版本在三张记忆表上创建的全部外键。"""

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT TABLE_NAME, CONSTRAINT_NAME
                FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS
                WHERE CONSTRAINT_SCHEMA = %s
                  AND TABLE_NAME IN (%s, %s, %s)
                  AND CONSTRAINT_TYPE = 'FOREIGN KEY'
                """,
                (
                    self._params.database,
                    _SESSION_TABLE,
                    _TURN_TABLE,
                    _GROUP_CONTEXT_TABLE,
                ),
            )
            constraints = cursor.fetchall()
            for constraint in constraints:
                table_name = constraint.get("TABLE_NAME")
                constraint_name = constraint.get("CONSTRAINT_NAME")
                if not isinstance(table_name, str) or not isinstance(
                    constraint_name,
                    str,
                ):
                    raise ChatMemorySchemaError("chat memory foreign key row is invalid")
                quoted_table = self._quote_identifier(table_name)
                quoted_constraint = self._quote_identifier(constraint_name)
                cursor.execute(f"ALTER TABLE {quoted_table} DROP FOREIGN KEY {quoted_constraint}")

    @staticmethod
    def _quote_identifier(value: str) -> str:
        """安全引用 INFORMATION_SCHEMA 返回的 MySQL 标识符。"""

        if not value or len(value) > 64 or "\x00" in value:
            raise ChatMemorySchemaError("chat memory foreign key identifier is invalid")
        return f"`{value.replace('`', '``')}`"

    def _open(self) -> Any:
        return self._connect(
            host=self._params.host,
            port=self._params.port,
            user=self._params.user,
            password=self._params.password,
            database=self._params.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )


class MySqlConversationMemoryStore:
    """Persist recent turns and compressed memory in MySQL transactions."""

    def __init__(
        self,
        params: DatabaseParams,
        *,
        policy: ConversationMemoryPolicy,
        connect: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定所选数据库、记忆策略和时钟，连接替身或共享池优先。"""
        self._params = params
        self._connect = connect or database_connector(params)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._policy_lock = threading.Lock()
        self._policy = policy

    @property
    def policy(self) -> ConversationMemoryPolicy:
        """Return the complete policy snapshot used by subsequent operations."""

        with self._policy_lock:
            return self._policy

    def update_policy(self, policy: ConversationMemoryPolicy) -> None:
        """Atomically replace runtime limits without replacing persisted data."""

        with self._policy_lock:
            self._policy = policy

    @property
    def history_turns(self) -> int:
        """读取当前近期历史开关，出站日志不得绕过关闭历史的策略。"""
        return self.policy.history_turns

    @property
    def max_history_chars(self) -> int:
        """读取模型历史预算，保留数据库中完整消息及素材原件。"""
        return self.policy.max_history_chars

    def context(self, session_id: str) -> ConversationContext:
        """读取并续期未过期会话，同时返回真实代际以隔离出站日志。"""

        normalized_session_id = _validate_session_id(session_id)
        policy = self.policy
        now = _naive_utc(self._clock())
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        if self._delete_expired_session(
                            cursor,
                            normalized_session_id,
                            now,
                        ):
                            connection.commit()
                            return ConversationContext(memory="", messages=())
                        row = self._lock_session(cursor, normalized_session_id)
                        if row is None:
                            connection.commit()
                            return ConversationContext(memory="", messages=())
                        expires_at = now + timedelta(seconds=policy.ttl_seconds)
                        self._renew_session(cursor, normalized_session_id, expires_at, now)
                        recent, _ = self._read_recent_turns(
                            cursor,
                            normalized_session_id,
                            compacted_through=int(row["compacted_through_turn_id"]),
                            turn_limit=policy.history_turns,
                            char_limit=policy.max_history_chars,
                        )
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemoryStoreError("failed to load conversation memory") from exc

        messages: list[ChatMessage] = []
        for _, turn in reversed(recent):
            messages.extend(
                (
                    ChatMessage(role="user", content=turn.user, history_request_id=turn.request_id),
                    ChatMessage(
                        role="assistant", content=turn.assistant, history_request_id=turn.request_id
                    ),
                )
            )
        memory = row["memory_summary"]
        if not isinstance(memory, str):
            raise ChatMemoryStoreError("conversation memory row is invalid")
        return ConversationContext(
            memory=memory, messages=tuple(messages), generation_id=str(row["generation_id"])
        )

    def ensure_generation(self, session_id: str) -> str:
        """事务内续期或创建空会话，为主动消息和后台结果提供稳定代际。"""
        normalized = _validate_session_id(session_id)
        now = _naive_utc(self._clock())
        expires_at = now + timedelta(seconds=self.policy.ttl_seconds)
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        self._delete_expired_session(cursor, normalized, now)
                        self._ensure_session(cursor, normalized, expires_at, now)
                        row = self._lock_session(cursor, normalized)
                        if row is None:
                            raise ChatMemoryStoreError("conversation session row is unavailable")
                        self._renew_session(cursor, normalized, expires_at, now)
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemoryStoreError("failed to ensure conversation generation") from exc
        return str(row["generation_id"])

    def commit_exchange(
        self,
        session_id: str,
        user: str,
        assistant: str,
        *,
        request_id: str | None = None,
    ) -> MemoryCompression | None:
        """在同一事务中保存请求回执和轮次，重复提交不会追加历史。"""

        normalized_session_id = _validate_session_id(session_id)
        request_id = validate_request_id(request_id)
        turn = ConversationTurn(
            user=_validate_text(user, field_name="user content"),
            assistant=_validate_text(assistant, field_name="assistant content"),
        )
        policy = self.policy
        if policy.history_turns == 0 and not policy.memory_enabled:
            return None
        now = _naive_utc(self._clock())
        expires_at = now + timedelta(seconds=policy.ttl_seconds)
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        if request_id is not None and not self._claim_request(
                            cursor, normalized_session_id, request_id, now
                        ):
                            connection.commit()
                            return None
                        self._delete_expired_session(cursor, normalized_session_id, now)
                        self._ensure_session(
                            cursor,
                            normalized_session_id,
                            expires_at,
                            now,
                        )
                        row = self._lock_session(cursor, normalized_session_id)
                        if row is None:
                            raise ChatMemoryStoreError("conversation session row is unavailable")
                        cursor.execute(
                            f"""
                            INSERT IGNORE INTO `{_TURN_TABLE}` (
                                session_id, request_id, user_content, assistant_content, create_time
                            ) VALUES (%s, %s, %s, %s, %s)
                            """,
                            (
                                normalized_session_id,
                                request_id,
                                turn.user,
                                turn.assistant,
                                now,
                            ),
                        )
                        compression = self._trim_locked(
                            cursor,
                            normalized_session_id,
                            row,
                            policy,
                        )
                        self._renew_session(cursor, normalized_session_id, expires_at, now)
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemoryStoreError("failed to commit conversation memory") from exc
        return compression

    def _claim_request(self, cursor: Any, session_id: str, request_id: str, now: datetime) -> bool:
        """回执独立保留一天，覆盖队列任务最大重试寿命及历史清空。"""
        expires_at = now + timedelta(seconds=REQUEST_RECEIPT_TTL_SECONDS)
        cursor.execute(
            f"INSERT IGNORE INTO `{_RECEIPT_TABLE}` "
            "(session_id, request_id, expires_at) VALUES (%s, %s, %s)",
            (session_id, request_id, expires_at),
        )
        if cursor.rowcount == 1:
            return True
        cursor.execute(
            f"UPDATE `{_RECEIPT_TABLE}` SET expires_at = %s "
            "WHERE session_id = %s AND request_id = %s AND expires_at <= %s",
            (expires_at, session_id, request_id, now),
        )
        return cursor.rowcount == 1

    def apply_memory(self, compression: MemoryCompression, summary: str) -> bool:
        """仅向同一会话代际及版本写回摘要，拒绝重置或过期前的结果。"""

        normalized_session_id = _validate_session_id(compression.session_id)
        normalized = summary.strip()
        if not normalized:
            return False
        policy = self.policy
        normalized = normalized[: policy.max_memory_chars]
        _validate_text(normalized, field_name="memory summary")
        now = _naive_utc(self._clock())
        expires_at = now + timedelta(seconds=policy.ttl_seconds)
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"""
                            UPDATE `{_SESSION_TABLE}`
                            SET memory_summary = %s, expires_at = %s, update_time = %s
                            WHERE session_id = %s
                              AND memory_revision = %s
                              AND generation_id = %s
                              AND expires_at > %s
                            """,
                            (
                                normalized,
                                expires_at,
                                now,
                                normalized_session_id,
                                compression.revision,
                                compression.generation_id,
                                now,
                            ),
                        )
                        applied = cursor.rowcount == 1
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemoryStoreError("failed to update conversation summary") from exc
        return applied

    def clear(self, session_id: str) -> bool:
        """Delete a conversation and its turns without foreign-key cascades."""

        normalized_session_id = _validate_session_id(session_id)
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        row = self._lock_session(cursor, normalized_session_id)
                        cursor.execute(
                            f"DELETE FROM `{_TURN_TABLE}` WHERE session_id = %s",
                            (normalized_session_id,),
                        )
                        if row is None:
                            deleted = False
                        else:
                            cursor.execute(
                                f"DELETE FROM `{_SESSION_TABLE}` WHERE session_id = %s",
                                (normalized_session_id,),
                            )
                            deleted = cursor.rowcount == 1
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemoryStoreError("failed to clear conversation memory") from exc
        return deleted

    def cleanup_expired(self, *, limit: int = _MAX_CLEANUP_ROWS) -> int:
        """Delete a bounded batch of expired sessions and turns explicitly."""

        _validate_cleanup_limit(limit)
        now = _naive_utc(self._clock())
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        self._cleanup_receipts(cursor, now, limit)
                        cursor.execute(
                            f"""
                            SELECT session_id
                            FROM `{_SESSION_TABLE}`
                            WHERE expires_at <= %s
                            ORDER BY expires_at
                            LIMIT %s
                            FOR UPDATE
                            """,
                            (now, limit),
                        )
                        rows = cursor.fetchall()
                        session_ids: list[str] = []
                        for row in rows:
                            session_id = row["session_id"]
                            if not isinstance(session_id, str):
                                raise ChatMemoryStoreError("conversation session row is invalid")
                            session_ids.append(session_id)
                        if not session_ids:
                            deleted = 0
                        else:
                            placeholders = ",".join("%s" for _ in session_ids)
                            cursor.execute(
                                f"DELETE FROM `{_TURN_TABLE}` WHERE session_id IN ({placeholders})",
                                tuple(session_ids),
                            )
                            cursor.execute(
                                f"DELETE FROM `{_SESSION_TABLE}` "
                                f"WHERE session_id IN ({placeholders})",
                                tuple(session_ids),
                            )
                            deleted = max(0, cursor.rowcount)
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemoryStoreError("failed to clean expired conversation memory") from exc
        return deleted

    def _cleanup_receipts(self, cursor: Any, now: datetime, limit: int) -> None:
        """按独立过期时间批量清理回执，不随历史和摘要删除。"""
        cursor.execute(
            f"SELECT session_id, request_id FROM `{_RECEIPT_TABLE}` "
            "WHERE expires_at <= %s ORDER BY expires_at LIMIT %s FOR UPDATE",
            (now, limit),
        )
        for row in cursor.fetchall():
            cursor.execute(
                f"DELETE FROM `{_RECEIPT_TABLE}` WHERE session_id = %s AND request_id = %s",
                (row["session_id"], row["request_id"]),
            )

    def _trim_locked(
        self,
        cursor: Any,
        session_id: str,
        row: Mapping[str, object] | Any,
        policy: ConversationMemoryPolicy,
    ) -> MemoryCompression | None:
        compacted_through = int(row["compacted_through_turn_id"])
        _, overflow_id = self._read_recent_turns(
            cursor,
            session_id,
            compacted_through=compacted_through,
            turn_limit=policy.history_turns,
            char_limit=policy.max_history_chars,
        )
        if overflow_id is None:
            return None

        target_turns = policy.history_turns
        target_chars = policy.max_history_chars
        if policy.memory_enabled:
            target_turns = (
                0
                if policy.history_turns == 0
                else max(
                    1,
                    policy.history_turns // 2,
                )
            )
            target_chars = max(1, policy.max_history_chars // 2)
        _, new_boundary = self._read_recent_turns(
            cursor,
            session_id,
            compacted_through=compacted_through,
            turn_limit=target_turns,
            char_limit=target_chars,
        )
        if new_boundary is None:
            raise ChatMemoryStoreError("conversation trim boundary is invalid")

        if not policy.memory_enabled:
            cursor.execute(
                f"""
                UPDATE `{_SESSION_TABLE}`
                SET compacted_through_turn_id = %s
                WHERE session_id = %s
                """,
                (new_boundary, session_id),
            )
            return None

        previous_memory = row["memory_summary"]
        if not isinstance(previous_memory, str):
            raise ChatMemoryStoreError("conversation memory row is invalid")
        evicted = self._read_evicted_turns(
            cursor,
            session_id,
            after_id=compacted_through,
            through_id=new_boundary,
        )
        revision = int(row["memory_revision"]) + 1
        compression = MemoryCompression(
            session_id=session_id,
            revision=revision,
            previous_memory=previous_memory,
            turns=tuple(evicted),
            generation_id=str(row["generation_id"]),
        )
        fallback = _fallback_memory(compression, policy.max_memory_chars)
        cursor.execute(
            f"""
            UPDATE `{_SESSION_TABLE}`
            SET memory_summary = %s,
                memory_revision = %s,
                compacted_through_turn_id = %s
            WHERE session_id = %s
            """,
            (fallback, revision, new_boundary, session_id),
        )
        return compression

    def _read_recent_turns(
        self,
        cursor: Any,
        session_id: str,
        *,
        compacted_through: int,
        turn_limit: int,
        char_limit: int,
    ) -> tuple[list[tuple[int, ConversationTurn]], int | None]:
        recent: list[tuple[int, ConversationTurn]] = []
        total_chars = 0
        before_id: int | None = None
        while True:
            before_clause = "" if before_id is None else "AND id < %s"
            parameters: tuple[object, ...] = (session_id, compacted_through)
            if before_id is not None:
                parameters += (before_id,)
            parameters += (_READ_BATCH_SIZE,)
            cursor.execute(
                f"""
                SELECT id, request_id, user_content, assistant_content
                FROM `{_TURN_TABLE}`
                WHERE session_id = %s AND id > %s {before_clause}
                ORDER BY id DESC
                LIMIT %s
                """,
                parameters,
            )
            rows = cursor.fetchall()
            if not rows:
                return recent, None
            for row in rows:
                turn_id, turn = _conversation_turn_from_row(row)
                if len(recent) >= turn_limit or total_chars + turn.size > char_limit:
                    return recent, turn_id
                recent.append((turn_id, turn))
                total_chars += turn.size
            if len(rows) < _READ_BATCH_SIZE:
                return recent, None
            before_id = int(rows[-1]["id"])

    def _read_evicted_turns(
        self,
        cursor: Any,
        session_id: str,
        *,
        after_id: int,
        through_id: int,
    ) -> list[ConversationTurn]:
        turns: list[ConversationTurn] = []
        source_chars = 0
        before_id = through_id + 1
        while True:
            cursor.execute(
                f"""
                SELECT id, request_id, user_content, assistant_content
                FROM `{_TURN_TABLE}`
                WHERE session_id = %s AND id > %s AND id <= %s AND id < %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (
                    session_id,
                    after_id,
                    through_id,
                    before_id,
                    _READ_BATCH_SIZE,
                ),
            )
            rows = cursor.fetchall()
            if not rows:
                break
            for row in rows:
                turn_id, turn = _conversation_turn_from_row(row)
                if source_chars + turn.size > _MAX_COMPRESSION_SOURCE_CHARS:
                    turns.reverse()
                    return turns
                turns.append(turn)
                source_chars += turn.size
                before_id = turn_id
            if len(rows) < _READ_BATCH_SIZE:
                break
        turns.reverse()
        return turns

    @staticmethod
    def _delete_expired_session(cursor: Any, session_id: str, now: datetime) -> bool:
        """锁定并显式删除一个过期会话及其轮次。"""

        cursor.execute(
            f"""
            SELECT session_id
            FROM `{_SESSION_TABLE}`
            WHERE session_id = %s AND expires_at <= %s
            FOR UPDATE
            """,
            (session_id, now),
        )
        if cursor.fetchone() is None:
            return False
        cursor.execute(
            f"DELETE FROM `{_TURN_TABLE}` WHERE session_id = %s",
            (session_id,),
        )
        cursor.execute(
            f"DELETE FROM `{_SESSION_TABLE}` WHERE session_id = %s",
            (session_id,),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _ensure_session(
        cursor: Any,
        session_id: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        cursor.execute(
            f"""
            INSERT IGNORE INTO `{_SESSION_TABLE}` (
                session_id, generation_id, memory_summary, memory_revision,
                compacted_through_turn_id, expires_at, create_time, update_time
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (session_id, uuid.uuid4().hex, "", 0, 0, expires_at, now, now),
        )

    @staticmethod
    def _lock_session(cursor: Any, session_id: str) -> Mapping[str, object] | Any | None:
        cursor.execute(
            f"""
            SELECT memory_summary, memory_revision, generation_id,
                   compacted_through_turn_id, expires_at
            FROM `{_SESSION_TABLE}`
            WHERE session_id = %s
            FOR UPDATE
            """,
            (session_id,),
        )
        return cursor.fetchone()

    @staticmethod
    def _renew_session(
        cursor: Any,
        session_id: str,
        expires_at: datetime,
        now: datetime,
    ) -> None:
        cursor.execute(
            f"""
            UPDATE `{_SESSION_TABLE}`
            SET expires_at = %s, update_time = %s
            WHERE session_id = %s
            """,
            (expires_at, now, session_id),
        )

    def _open(self) -> Any:
        return self._connect(
            host=self._params.host,
            port=self._params.port,
            user=self._params.user,
            password=self._params.password,
            database=self._params.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )


class MySqlRecentGroupContextStore:
    """通过所选数据库的短事务读写有界群聊 JSON 快照。"""

    def __init__(
        self,
        params: DatabaseParams,
        *,
        secret: str,
        policy: GroupContextPolicy,
        connect: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        """绑定所选数据库和匿名作用域密钥，群上下文策略可在线更新。"""
        if not secret:
            raise ValueError("group context secret cannot be empty")
        self._params = params
        self._secret = secret
        self._connect = connect or database_connector(params)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._policy_lock = threading.Lock()
        self._policy = policy

    @property
    def policy(self) -> GroupContextPolicy:
        """Return the complete policy snapshot used by subsequent operations."""

        with self._policy_lock:
            return self._policy

    def update_policy(self, policy: GroupContextPolicy) -> None:
        """Atomically replace limits without deleting the persisted snapshot."""

        with self._policy_lock:
            self._policy = policy

    def record(
        self,
        *,
        bot_id: str,
        group_id: str,
        sender_id: str,
        sender_name: str | None,
        content: str,
        delivery_id: str | None = None,
    ) -> None:
        """短事务追加群文本；可选出站投递标识随快照去重，兼容旧成员消息。"""

        policy = self.policy
        message = _normalize_group_message(
            sender_id=sender_id,
            sender_name=sender_name,
            content=content,
            delivery_id=delivery_id,
        )
        message = _fit_group_message(message, policy.max_chars)
        if message is None:
            return
        scope_id = self._scope_id(bot_id, group_id)
        now = _naive_utc(self._clock())
        expires_at = now + timedelta(seconds=policy.ttl_seconds)
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        self._delete_expired(cursor, scope_id, now)
                        cursor.execute(
                            f"""
                            INSERT IGNORE INTO `{_GROUP_CONTEXT_TABLE}` (
                                scope_id, messages_json, expires_at, create_time, update_time
                            ) VALUES (%s, %s, %s, %s, %s)
                            """,
                            (scope_id, "[]", expires_at, now, now),
                        )
                        row = self._lock_group(cursor, scope_id)
                        if row is None:
                            raise ChatMemoryStoreError("group context row is unavailable")
                        messages = _decode_group_messages(row["messages_json"])
                        if delivery_id is not None and any(
                            item.delivery_id == delivery_id for item in messages
                        ):
                            connection.commit()
                            return
                        messages.append(message)
                        messages = _trim_group_messages(messages, policy)
                        encoded = _encode_group_messages(messages)
                        cursor.execute(
                            f"""
                            UPDATE `{_GROUP_CONTEXT_TABLE}`
                            SET messages_json = %s, expires_at = %s, update_time = %s
                            WHERE scope_id = %s
                            """,
                            (encoded, expires_at, now, scope_id),
                        )
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemoryStoreError("failed to persist group context") from exc

    def context(self, *, bot_id: str, group_id: str) -> str | None:
        """保留普通聊天读取群背景的原接口及既有续期策略。"""
        return self.snapshot(bot_id=bot_id, group_id=group_id).context

    def snapshot(self, *, bot_id: str, group_id: str) -> RecentGroupSnapshot:
        """在同一事务内读取背景和最后消息，避免定时输入与群背景发生版本偏差。"""

        policy = self.policy
        scope_id = self._scope_id(bot_id, group_id)
        now = _naive_utc(self._clock())
        expires_at = now + timedelta(seconds=policy.ttl_seconds)
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        if self._delete_expired(cursor, scope_id, now):
                            connection.commit()
                            return RecentGroupSnapshot(None, None)
                        row = self._lock_group(cursor, scope_id)
                        if row is None:
                            connection.commit()
                            return RecentGroupSnapshot(None, None)
                        messages = _trim_group_messages(
                            _decode_group_messages(row["messages_json"]),
                            policy,
                        )
                        encoded = _encode_group_messages(messages)
                        cursor.execute(
                            f"""
                            UPDATE `{_GROUP_CONTEXT_TABLE}`
                            SET messages_json = %s, expires_at = %s, update_time = %s
                            WHERE scope_id = %s
                            """,
                            (encoded, expires_at, now, scope_id),
                        )
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemoryStoreError("failed to load group context") from exc
        if not messages:
            return RecentGroupSnapshot(None, None)
        return RecentGroupSnapshot(
            "\n".join(message.render() for message in messages), messages[-1],
        )

    def cleanup_expired(self, *, limit: int = _MAX_CLEANUP_ROWS) -> int:
        """Delete a bounded batch of expired group snapshots."""

        _validate_cleanup_limit(limit)
        now = _naive_utc(self._clock())
        try:
            with closing(self._open()) as connection:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            f"""
                            DELETE FROM `{_GROUP_CONTEXT_TABLE}`
                            WHERE scope_id IN (
                                SELECT scope_id FROM (
                                    SELECT scope_id
                                    FROM `{_GROUP_CONTEXT_TABLE}`
                                    WHERE expires_at <= %s
                                    ORDER BY expires_at
                                    LIMIT %s
                                ) AS expired_groups
                            )
                            """,
                            (now, limit),
                        )
                        deleted = max(0, cursor.rowcount)
                    connection.commit()
                except Exception:
                    with suppress(Exception):
                        connection.rollback()
                    raise
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise ChatMemoryStoreError("failed to clean expired group context") from exc
        return deleted

    def _scope_id(self, bot_id: str, group_id: str) -> str:
        normalized_bot_id = _validate_numeric_id(bot_id, "bot id")
        normalized_group_id = _validate_numeric_id(group_id, "group id")
        return external_session_id(
            f"bot:{normalized_bot_id}:group:{normalized_group_id}:recent",
            self._secret,
        )

    @staticmethod
    def _delete_expired(cursor: Any, scope_id: str, now: datetime) -> bool:
        cursor.execute(
            f"""
            DELETE FROM `{_GROUP_CONTEXT_TABLE}`
            WHERE scope_id = %s AND expires_at <= %s
            """,
            (scope_id, now),
        )
        return cursor.rowcount == 1

    @staticmethod
    def _lock_group(cursor: Any, scope_id: str) -> Mapping[str, object] | Any | None:
        cursor.execute(
            f"""
            SELECT messages_json, expires_at
            FROM `{_GROUP_CONTEXT_TABLE}`
            WHERE scope_id = %s
            FOR UPDATE
            """,
            (scope_id,),
        )
        return cursor.fetchone()

    def _open(self) -> Any:
        return self._connect(
            host=self._params.host,
            port=self._params.port,
            user=self._params.user,
            password=self._params.password,
            database=self._params.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )


def _conversation_turn_from_row(row: Mapping[str, object] | Any) -> tuple[int, ConversationTurn]:
    turn_id = row["id"]
    user = row["user_content"]
    assistant = row["assistant_content"]
    if type(turn_id) is not int or not isinstance(user, str) or not isinstance(assistant, str):
        raise ChatMemoryStoreError("conversation turn row is invalid")
    return turn_id, ConversationTurn(user=user, assistant=assistant, request_id=row["request_id"])


def _validate_session_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_SESSION_ID_LENGTH
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise ChatMemoryValidationError("session id is invalid")
    return value


def _validate_text(value: str, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ChatMemoryValidationError(f"{field_name} must be text")
    if len(value.encode("utf-8")) > _MAX_TEXT_BYTES:
        raise ChatMemoryValidationError(f"{field_name} exceeds size limit")
    return value


def _validate_numeric_id(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ChatMemoryValidationError(f"{field_name} is invalid")
    normalized = value.strip()
    if not normalized.isascii() or not normalized.isdigit() or not 1 <= len(normalized) <= 20:
        raise ChatMemoryValidationError(f"{field_name} is invalid")
    return normalized


def _normalize_group_message(
    *,
    sender_id: str,
    sender_name: str | None,
    content: str,
    delivery_id: str | None = None,
) -> RecentGroupMessage:
    """规范成员及确认出站消息，投递标识只用于群快照中的幂等追加。"""
    normalized_sender_id = _validate_numeric_id(sender_id, "sender id")
    if sender_name is not None and not isinstance(sender_name, str):
        raise ChatMemoryValidationError("sender name must be text")
    if sender_name is not None and len(sender_name.encode("utf-8")) > 512:
        raise ChatMemoryValidationError("sender name exceeds size limit")
    normalized_content = _validate_text(content, field_name="group content")
    return RecentGroupMessage(
        sender_id=normalized_sender_id,
        sender_name=sender_name,
        content=normalized_content,
        delivery_id=delivery_id,
    )


def _fit_group_message(
    message: RecentGroupMessage,
    max_chars: int,
) -> RecentGroupMessage | None:
    """裁剪可见正文时保留投递标识，避免预算变化破坏已发送消息去重。"""
    rendered = message.render()
    if len(rendered) <= max_chars:
        return message
    content_chars = max_chars - (len(rendered) - len(message.content))
    if content_chars <= 0:
        return None
    return RecentGroupMessage(
        sender_id=message.sender_id,
        sender_name=message.sender_name,
        content=message.content[:content_chars],
        delivery_id=message.delivery_id,
    )


def _trim_group_messages(
    messages: list[RecentGroupMessage],
    policy: GroupContextPolicy,
) -> list[RecentGroupMessage]:
    while messages and (
        len(messages) > policy.message_limit or _rendered_group_size(messages) > policy.max_chars
    ):
        messages.pop(0)
    return messages


def _rendered_group_size(messages: Sequence[RecentGroupMessage]) -> int:
    return sum(len(message.render()) for message in messages) + max(0, len(messages) - 1)


def _decode_group_messages(value: object) -> list[RecentGroupMessage]:
    """兼容原成员消息 JSON，只为确认出站消息允许可选投递标识。"""
    if not isinstance(value, (str, bytes)):
        raise ChatMemoryStoreError("group context payload is invalid")
    try:
        raw = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ChatMemoryStoreError("group context payload is invalid") from exc
    if not isinstance(raw, list) or len(raw) > _MAX_GROUP_MESSAGE_ITEMS:
        raise ChatMemoryStoreError("group context payload is invalid")
    messages: list[RecentGroupMessage] = []
    try:
        for item in raw:
            required = {"sender_id", "sender_name", "content"}
            if (not isinstance(item, Mapping) or not required <= set(item)
                    or set(item) - required - {"delivery_id"}):
                raise ChatMemoryValidationError("group context item is invalid")
            messages.append(
                _normalize_group_message(
                    sender_id=item["sender_id"],
                    sender_name=item["sender_name"],
                    content=item["content"],
                    delivery_id=item.get("delivery_id"),
                )
            )
    except (KeyError, ValueError) as exc:
        raise ChatMemoryStoreError("group context payload is invalid") from exc
    return messages


def _encode_group_messages(messages: Sequence[RecentGroupMessage]) -> str:
    """成员消息保持旧格式，确认出站消息额外保留投递去重标识。"""
    encoded = json.dumps(
        [
            {
                "sender_id": message.sender_id,
                "sender_name": message.sender_name,
                "content": message.content,
                **({"delivery_id": message.delivery_id} if message.delivery_id is not None else {}),
            }
            for message in messages
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(encoded.encode("utf-8")) > _MAX_GROUP_PAYLOAD_BYTES:
        raise ChatMemoryStoreError("group context payload exceeds size limit")
    return encoded


def _validate_cleanup_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= _MAX_CLEANUP_ROWS:
        raise ValueError(f"cleanup limit must be between 1 and {_MAX_CLEANUP_ROWS}")


def _naive_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _fallback_memory(compression: MemoryCompression, max_chars: int) -> str:
    sections: list[str] = []
    if compression.previous_memory:
        sections.append(f"已有记忆：\n{compression.previous_memory}")
    sections.extend(f"用户：{turn.user}\n助手：{turn.assistant}" for turn in compression.turns)
    combined = "\n\n".join(sections).strip()
    return combined if len(combined) <= max_chars else combined[-max_chars:]
