"""合并授权事实的语义与词项命中，用来源及使用信息排序。"""

from __future__ import annotations

import asyncio
import math
import re
import time
import unicodedata
from datetime import datetime, timezone

from agent_bot.database_pool import finish_database_write
from agent_bot.integrations.memory_vectors import MemoryVectorIndex
from agent_bot.memory.embeddings import EmbeddingClient, EmbeddingError


def _terms(text: str) -> set[str]:
    """保留数字和实体片段，中文使用双字词项避免依赖额外分词服务。"""
    text = unicodedata.normalize("NFKC", text).casefold()
    terms = set(re.findall(r"[a-z]+(?:[-_.][a-z0-9]+)*|[0-9]+", text))
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        terms.update(phrase[index : index + 2] for index in range(max(1, len(phrase) - 1)))
    return terms


def _recency(value: object) -> float:
    """最近实际使用按三十天半衰期衰减，未知时间不伪造为最近使用。"""
    if value is None:
        return 0.0
    try:
        stamp = datetime.fromisoformat(value) if isinstance(value, str) else value
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        days = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds() / 86400)
        return math.exp(-math.log(2) * days / 30)
    except (ValueError, TypeError, AttributeError):
        raise EmbeddingError("记忆最近使用时间无效") from None


def _source_support(kind: str, source: str) -> float:
    """按事实类别衡量来源支持；个人关系偏好与可验证技术事实使用不同依据。"""
    if kind in {"preference", "relationship"}:
        weights = {
            "self_report": 1.0,
            "user_correction": 1.0,
            "admin_confirmation": 0.8,
            "tool_result": 0.5,
            "third_party": 0.2,
        }
    elif kind == "fact":
        weights = {
            "tool_result": 1.0,
            "admin_confirmation": 1.0,
            "user_correction": 0.9,
            "self_report": 0.6,
            "third_party": 0.3,
        }
    else:
        weights = {
            "self_report": 1.0,
            "user_correction": 1.0,
            "admin_confirmation": 0.9,
            "tool_result": 0.7,
            "third_party": 0.4,
        }
    # 未知旧来源始终最低；较高分只参与已相关事实排序，不能提升来源归属。
    return weights.get(source, 0.0)


def lexical_recall(query: str, candidates: list[dict]) -> list[dict]:
    """仅用正文、主题和标签的实际词项命中兜底，元信息不能引入无关事实。"""
    query_terms = _terms(query)
    if not query_terms:
        return []
    ranked = []
    for row in candidates:
        terms = _terms(" ".join([row["content"], row.get("topic", ""), *row.get("tags", [])]))
        matches = query_terms & terms
        if not matches:
            continue
        relevance = len(matches) / len(query_terms)
        importance = max(0.0, min(1.0, float(row.get("importance", 0.5))))
        score = (
            0.85 * relevance + 0.07 * importance
            + 0.05 * _source_support(row.get("kind", "fact"), row.get("source_kind", ""))
            + 0.03 * _recency(row.get("last_used_at"))
        )
        ranked.append({**row, "relevance": relevance, "score": score})
    return sorted(ranked, key=lambda row: (-row["score"], row["id"]))[:8]


class HybridMemorySearch:
    """先过滤授权有效版本，再合并语义和词项两路，元信息仅参与相关项排序。"""

    def __init__(self, index: MemoryVectorIndex, embedder: EmbeddingClient) -> None:
        """保存显式索引和模型服务，禁止自动选择其他检索后端。"""
        self._index = index
        self._embedder = embedder

    async def search(
        self, query: str, candidates: list[dict], deadline_at: float | None = None
    ) -> list[dict]:
        """截止时间覆盖所有批量请求，超时或取消不会返回部分降级结果。"""
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise EmbeddingError("记忆检索问题必须是1至200字符")
        if len(candidates) > 1024:
            raise EmbeddingError("记忆候选超出本次检索预算")
        if deadline_at is None:
            return await self._search(query, candidates, deadline_at)
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise EmbeddingError("本次记忆检索已超时")
        try:
            return await asyncio.wait_for(self._search(query, candidates, deadline_at), remaining)
        except TimeoutError as error:
            raise EmbeddingError("本次记忆检索已超时") from error

    @staticmethod
    def _check_deadline(deadline_at: float | None) -> None:
        """同步数据库阶段也核对截止时间，不在失去请求所有权后开始写向量。"""
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise EmbeddingError("本次记忆检索已超时")

    async def _search(
        self, query: str, candidates: list[dict], deadline_at: float | None
    ) -> list[dict]:
        """缓存按事实版本及目录版本查找，仅对缺失项请求批量向量。"""
        # 读检查同样持有数据库租用；取消必须等待已启动事务结束，不能遗留后台线程。
        candidates = await finish_database_write(lambda: self._index.current(candidates))
        self._check_deadline(deadline_at)
        if not candidates:
            return []
        query_batch = await self._embedder.embed((query,))
        vectors = await finish_database_write(
            lambda: self._index.load(candidates, query_batch.model_key),
        )
        self._check_deadline(deadline_at)
        missing = [row for row in candidates if (row["id"], row["revision"]) not in vectors]
        if missing:
            batch = await self._embedder.embed(tuple(row["content"] for row in missing))
            if batch.model_key != query_batch.model_key:
                raise EmbeddingError("Embedding 模型目录版本已变化，请重新检索")
            self._check_deadline(deadline_at)
            accepted = await finish_database_write(
                lambda: self._index.save(missing, batch, deadline_at=deadline_at),
            )
            vectors.update(
                {
                    (row["id"], row["revision"]): vector
                    for row, vector in zip(missing, batch.vectors, strict=True)
                    if (row["id"], row["revision"]) in accepted
                }
            )
        query_vector = query_batch.vectors[0]
        query_terms = _terms(query)
        ranked = []
        current = await finish_database_write(lambda: self._index.current(candidates))
        self._check_deadline(deadline_at)
        for row in current:
            vector = vectors.get((row["id"], row["revision"]))
            if vector is None:
                continue
            if len(vector) != len(query_vector):
                raise EmbeddingError("记忆缓存与查询向量维度不一致，请重建索引")
            semantic = max(
                -1.0, min(1.0, sum(a * b for a, b in zip(query_vector, vector, strict=True)))
            )
            terms = _terms(" ".join([row["content"], row.get("topic", ""), *row.get("tags", [])]))
            lexical = len(query_terms & terms) / len(query_terms) if query_terms else 0.0
            if semantic < 0.25 and lexical == 0:
                continue
            relevance = max(0.0, semantic)
            source = _source_support(row.get("kind", "fact"), row.get("source_kind", ""))
            importance = max(0.0, min(1.0, float(row.get("importance", 0.5))))
            score = (
                0.65 * relevance
                + 0.2 * lexical
                + 0.07 * importance
                + 0.05 * source
                + 0.03 * _recency(row.get("last_used_at"))
            )
            ranked.append({**row, "relevance": relevance, "score": score})
        self._check_deadline(deadline_at)
        return sorted(ranked, key=lambda row: (-row["score"], row["id"]))[:8]
