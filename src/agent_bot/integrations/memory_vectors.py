"""使用现有事务仓储保存版本化向量，索引本身不扩大候选授权范围。"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from agent_bot.memory.embeddings import EmbeddingBatch, EmbeddingError, normalized_vector

if TYPE_CHECKING:
    from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore


class MemoryVectorIndex:
    """只读写调用者提供的授权候选，写入及返回前复核当前版本。"""

    def __init__(self, store: TaggedMemoryStore) -> None:
        """借用既有数据库及事务生命周期，不自动建表或迁移。"""
        self._store = store

    @staticmethod
    def _current(cursor: Any, candidates: list[dict]) -> list[dict]:
        """一次批量核对当前状态与生效窗口，失效和墓碑不会进入索引。"""
        if not candidates:
            return []
        stamp = datetime.now(timezone.utc).replace(tzinfo=None)
        ids = sorted({row["id"] for row in candidates})
        cursor.execute(
            "SELECT id,revision FROM chat_tagged_memory WHERE id IN ("
            + ",".join("%s" for _ in ids)
            + ") AND status='active' AND is_deleted=0 "
            "AND (valid_from IS NULL OR valid_from<=%s) "
            "AND (valid_until IS NULL OR valid_until>%s) FOR UPDATE", (*ids, stamp, stamp),
        )
        current = {(row["id"], row["revision"]) for row in cursor.fetchall()}
        return [row for row in candidates if (row["id"], row["revision"]) in current]

    def current(self, candidates: list[dict]) -> list[dict]:
        """返回仍有效的原候选，保留服务器绑定的身份和来源信息。"""
        with self._store._transaction() as cursor:
            return self._current(cursor, candidates)

    def load(self, candidates: list[dict], model_key: str) -> dict[tuple[int, int], tuple]:
        """只取当前有效且模型版本完全匹配的缓存，不扫描其他作用域。"""
        with self._store._transaction() as cursor:
            rows = self._current(cursor, candidates)
            if not rows:
                return {}
            ids = [row["id"] for row in rows]
            cursor.execute(
                "SELECT memory_id,revision,vector_json FROM chat_memory_embedding "
                "WHERE model_key=%s AND memory_id IN (" + ",".join("%s" for _ in ids) + ")",
                (model_key, *ids),
            )
            permitted = {(row["id"], row["revision"]) for row in rows}
            result = {}
            for row in cursor.fetchall():
                key = (row["memory_id"], row["revision"])
                if key in permitted:
                    try:
                        value = json.loads(row["vector_json"])
                    except (ValueError, TypeError) as error:
                        raise EmbeddingError("记忆向量缓存格式无效，请重建索引") from error
                    result[key] = normalized_vector(value)
            return result

    @staticmethod
    def _check_deadline(deadline_at: float | None) -> None:
        """事务超时必须抛出使既有事务管理器回滚，不能留下迟到向量。"""
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise EmbeddingError("本次记忆向量写入已超时")

    def save(self, candidates: list[dict], batch: EmbeddingBatch,
             deadline_at: float | None = None) -> set[tuple[int, int]]:
        """锁定当前事实后保存，计算过程中发生的修订不会重新写入旧版本。"""
        if len(candidates) != len(batch.vectors):
            raise EmbeddingError("记忆候选与向量条数不一致")
        self._check_deadline(deadline_at)
        with self._store._transaction() as cursor:
            permitted = {(row["id"], row["revision"])
                         for row in self._current(cursor, candidates)}
            stamp = datetime.now(timezone.utc).replace(tzinfo=None)
            for row, vector in zip(candidates, batch.vectors, strict=True):
                self._check_deadline(deadline_at)
                if (row["id"], row["revision"]) not in permitted:
                    continue
                # 不使用可替换主键的 REPLACE，保留同版本第一次生成的缓存快照。
                cursor.execute(
                    "SELECT memory_id FROM chat_memory_embedding "
                    "WHERE memory_id=%s AND revision=%s AND model_key=%s",
                    (row["id"], row["revision"], batch.model_key),
                )
                if cursor.fetchone() is None:
                    cursor.execute(
                        "INSERT INTO chat_memory_embedding "
                        "(memory_id,revision,model_key,vector_json,created_at) "
                        "VALUES (%s,%s,%s,%s,%s)",
                        (row["id"], row["revision"], batch.model_key, json.dumps(vector), stamp),
                    )
            self._check_deadline(deadline_at)
            return permitted
