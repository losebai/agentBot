"""在会话事务内复核摘要引用，防止遗忘或修订后旧正文通过摘要复述。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from agent_bot.ai.database import query
from agent_bot.memory.policy import effective_status, utc_now


class MemorySummaryFilter:
    """证据事件、来源会话和匿名主体共同授权；不使用正文关键词猜测关联。"""

    def __init__(self, *, clock: Callable[[], datetime] = utc_now) -> None:
        """复用调用方事务连接，测试可固定有效期边界而不连接外部服务。"""
        self._clock = clock

    def __call__(
        self, connection: Any, session_id: str, entries: tuple[dict, ...],
    ) -> tuple[dict, ...]:
        """写回及注入前读取当前根事实；任何相关失效来源保守移除整条摘要。"""
        event_ids = tuple(dict.fromkeys(ref for entry in entries
                                        for ref in entry.get("source_event_refs", ())))
        evidence: dict[str, list[dict]] = {}
        if event_ids:
            # 锁定来源根事实直到会话事务完成，避免核对之后、摘要提交之前被并发遗忘。
            rows = query(
                connection,
                "SELECT m.id,m.revision,m.status,m.is_deleted,m.scope_id,m.scope,"
                "m.subject_id,m.subject_type,m.valid_from,m.valid_until,"
                "e.first_revision,e.source_actor_id,e.source_session_id,e.source_event_id "
                "FROM chat_memory_evidence e JOIN chat_tagged_memory m ON m.id=e.memory_id "
                "WHERE e.source_event_id IN (" + ",".join("%s" for _ in event_ids)
                + ") FOR UPDATE", event_ids,
            )
            for row in rows:
                evidence.setdefault(row["source_event_id"], []).append(row)
        result = []
        now = self._clock()
        for entry in entries:
            rows = [row for ref in entry.get("source_event_refs", ())
                    for row in evidence.get(ref, ())]
            if any(not self._authorized_current(row, entry, session_id, now) for row in rows):
                continue
            refs = entry.get("memory_refs", ())
            if any(not self._verified_memory_ref(ref, rows) for ref in refs):
                continue
            if entry.get("source_kind", "unknown") == "unknown" or entry.get(
                "subject_id", "unknown",
            ) == "unknown":
                # 旧摘要来源不能靠模型补造；保留历史背景时也不宣称是本人偏好。
                entry = {**entry, "kind": "context", "subject_id": "unknown"}
            result.append(entry)
        return tuple(result)

    @staticmethod
    def _authorized_current(row: dict, entry: dict, session_id: str, now: datetime) -> bool:
        """同事件的多条事实一并复核，无法证明其权限或版本时不暴露旧摘要正文。"""
        subject = entry.get("subject_id", "unknown")
        return (
            not row["is_deleted"] and effective_status(row, now) == "active"
            and row["first_revision"] == row["revision"]
            and row["source_session_id"] == session_id
            and subject != "unknown" and row["source_actor_id"] == subject
            and (row["scope"] != "user" or row["scope_id"] == subject)
            and (row["subject_type"] != "user" or row["subject_id"] == subject)
        )

    @staticmethod
    def _verified_memory_ref(reference: dict, rows: list[dict]) -> bool:
        """仅接受本条真实证据授权且仍为当前版本的记忆，孤立ID不授予读取权。"""
        if not isinstance(reference, dict):
            return False
        identity, revision = reference.get("id"), reference.get("revision")
        if isinstance(identity, bool) or not isinstance(identity, int) or identity < 1:
            return False
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            return False
        return any(row["id"] == identity and row["revision"] == revision for row in rows)
