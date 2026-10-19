"""显式导入旧会话历史，不删除旧表、不覆盖已有统一上下文。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

import pymysql

from agent_bot.database import database_connector
from agent_bot.settings import DatabaseParams


def _stamp(value: datetime | str) -> datetime:
    """旧会话表采用 UTC，字符串与驱动时间统一为带时区时间供排序。"""
    stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    return stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)


def _rows(connection: Any, table: str, backend: str) -> list[dict]:
    """仅枚举固定的历史表，允许已确认的缺失出站表部署直接迁移。"""
    if table not in {"chat_memory_session", "chat_memory_turn", "chat_sent_message"}:
        raise ValueError("unsupported legacy context table")
    with connection.cursor() as cursor:
        if backend == "sqlite":
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=%s", (table,),
            )
        else:
            cursor.execute(
                "SELECT TABLE_NAME FROM information_schema.tables "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=%s", (table,),
            )
        if cursor.fetchone() is None:
            return []
        cursor.execute(f"SELECT * FROM {table}")
        return list(cursor.fetchall())


def migrate_session_context(
    params: DatabaseParams, *, connect: Callable[..., Any] | None = None, dry_run: bool = False,
) -> dict[str, int]:
    """逐会话原子导入；部署须停服后执行，重跑不会覆盖已迁移或新产生的上下文。"""
    from agent_bot.session_context import SessionContextPolicy, SessionContextStore

    connector = connect or database_connector(params)
    with closing(connector(
        host=params.host, port=params.port, user=params.user, password=params.password,
        database=params.database, charset="utf8mb4", cursorclass=pymysql.cursors.DictCursor,
        autocommit=False, connect_timeout=5, read_timeout=10, write_timeout=10,
    )) as connection:
        # 预检目标表，结构未升级不能创建部分迁移结果。
        with connection.cursor() as cursor:
            cursor.execute("SELECT session_id FROM chat_session_context LIMIT 0")
        sessions = {row["session_id"]: row
                    for row in _rows(connection, "chat_memory_session", params.backend)}
        turns: dict[str, list[dict]] = defaultdict(list)
        sent: dict[str, list[dict]] = defaultdict(list)
        for row in _rows(connection, "chat_memory_turn", params.backend):
            turns[row["session_id"]].append(row)
        for row in _rows(connection, "chat_sent_message", params.backend):
            sent[row["session_id"]].append(row)
        connection.rollback()

    # 导入保留原始消息，不按工具默认窗口裁剪；运行时会使用用户当前配置限制模型视图。
    store = SessionContextStore(
        params, connect=connector,
        policy=SessionContextPolicy(
            history_turns=100_000, max_history_chars=100_000_000,
            memory_enabled=False, max_memory_chars=8000,
        ),
    )
    counts = {"sessions": 0, "imported": 0, "skipped": 0, "messages": 0}
    for session_id in sorted(sessions.keys() | turns.keys() | sent.keys()):
        previous = sessions.get(session_id)
        records = sorted(sent[session_id], key=lambda row: (_stamp(row["created_at"]), row["id"]))
        generation = (str(previous["generation_id"]) if previous is not None else
                      str(records[-1]["generation_id"]) if records else
                      hashlib.sha256(session_id.encode()).hexdigest()[:32])
        delivered_requests = {row["request_id"] for row in records
                              if row["generation_id"] == generation
                              and row["status"] == "sent" and row["request_id"] is not None}
        events: list[dict] = []
        boundary = int(previous["compacted_through_turn_id"]) if previous is not None else 0
        for row in sorted(turns[session_id], key=lambda row: row["id"]):
            if row["id"] <= boundary:
                continue
            request_id = row["request_id"] or f"legacy-turn-{row['id']}"
            common = {"request_id": request_id, "created_at": _stamp(row["create_time"]),
                      "material_ids": ()}
            events.append({**common, "role": "user", "content": row["user_content"]})
            if row["request_id"] not in delivered_requests:
                events.append({**common, "role": "assistant", "content": row["assistant_content"]})
        for row in records:
            material_ids = row["material_ids"]
            events.append({
                "role": "assistant", "content": row["content"],
                "request_id": row["request_id"], "created_at": _stamp(row["created_at"]),
                "material_ids": tuple(json.loads(material_ids))
                if isinstance(material_ids, str) else tuple(material_ids),
                "delivery_id": row["delivery_id"], "message_id": row["message_id"],
                "status": row["status"], "generation_id": row["generation_id"],
            })
        events.sort(key=lambda event: event["created_at"])
        counts["sessions"] += 1
        counts["messages"] += len(events)
        if not dry_run:
            imported = store.import_history(
                session_id, generation_id=generation,
                memory_summary=previous["memory_summary"] if previous is not None else "",
                events=events,
            )
            counts["imported" if imported else "skipped"] += 1
    return counts
