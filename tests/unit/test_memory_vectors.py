"""用隔离 SQLite 和模拟 HTTP 验证向量协议、版本隔离和混合召回。"""

import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from agent_bot.config import PluginConfig
from agent_bot.integrations.memory_vectors import MemoryVectorIndex
from agent_bot.memory.embeddings import EmbeddingBatch, EmbeddingClient, EmbeddingError
from agent_bot.memory.retrieval import HybridMemorySearch
from agent_bot.settings import DatabaseParams


class Catalog:
    """提供已明确选定的目录快照，不访问外部供应商或配置库。"""

    def __init__(self, protocol="openai-chat"):
        """构造协议完整且含内部凭据的目录记录。"""
        self.snapshot = SimpleNamespace(
            model=SimpleNamespace(id=1, revision=1, upstream_model="embedding-test"),
            provider=SimpleNamespace(
                id=2,
                revision=1,
                protocol=protocol,
                base_url="https://offline.invalid/v1",
                user_agent="AgentBot-Memory-Test/1.0",
                credential=SecretStr("private-test-key"),
                credential_available=True,
            ),
        )

    async def resolve(self, alias):
        """仅允许测试指定的模型别名，错误切换即失败。"""
        assert alias == "memory-embedding"
        return self.snapshot


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses", "ollama"])
async def test_embedding_protocol_and_order(protocol):
    """两种显式协议按输入次序返回，OpenAI 响应索引不能当作列表次序。"""

    def respond(request):
        """核对线上边界协议并故意将 OpenAI 响应倒序。"""
        body = json.loads(request.content)
        assert body["model"] == "embedding-test"
        assert body["input"] == ["甲", "乙"]
        assert request.headers["authorization"] == "Bearer private-test-key"
        assert request.headers["user-agent"] == "AgentBot-Memory-Test/1.0"
        if protocol == "ollama":
            assert request.url.path == "/v1/api/embed"
            assert body["truncate"] is False
            return httpx.Response(200, json={"embeddings": [[3, 4], [0, 2]]})
        assert request.url.path == "/v1/embeddings"
        assert body["encoding_format"] == "float"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0, 2]},
                    {"index": 0, "embedding": [3, 4]},
                ]
            },
        )

    client = EmbeddingClient(
        Catalog(protocol), "memory-embedding", transport=httpx.MockTransport(respond)
    )
    batch = await client.embed(("甲", "乙"))
    assert batch.vectors == ((0.6, 0.8), (0.0, 1.0))
    assert "private-test-key" not in batch.model_key


@pytest.mark.parametrize(
    "vectors", [[[1, 0], [1]], [[0, 0]], [[float("inf"), 1]], [[True, 1]], [[]]]
)
def test_invalid_embedding_batch_rejected(vectors):
    """错误维度、零向量及非有限数不能进入缓存和相似度排序。"""
    with pytest.raises(EmbeddingError):
        EmbeddingBatch("model-a", tuple(tuple(vector) for vector in vectors))


async def test_http_failure_does_not_try_another_protocol():
    """供应商返回错误必须明确失败，不能再调用另一条端点。"""
    requests = []

    def fail(request):
        """记录实际请求，错误正文故意含密钥验证不会泄漏。"""
        requests.append(request.url.path)
        return httpx.Response(503, text="private-test-key")

    with pytest.raises(EmbeddingError) as caught:
        await EmbeddingClient(
            Catalog(), "memory-embedding", transport=httpx.MockTransport(fail)
        ).embed(("事实",))
    assert requests == ["/v1/embeddings"]
    assert "private-test-key" not in str(caught.value)


@pytest.mark.parametrize(
    "body",
    [
        {"data": [{"index": 0, "embedding": 17}]},
        {"data": [{"index": 0, "embedding": [1, 0]}, {"index": 0, "embedding": [1, 0]}]},
        {"data": []},
        {"data": None},
    ],
)
async def test_malformed_upstream_response_has_public_error(body):
    """上游畸形结果都成为固定服务错误，不能抛出内部类型异常。"""
    client = EmbeddingClient(
        Catalog(),
        "memory-embedding",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json=body)),
    )
    with pytest.raises(EmbeddingError):
        await client.embed(("事实",))


async def test_batch_requests_have_explicit_bounds():
    """大批量按条数分组，超出整体预算必须在调用供应商前失败。"""
    sizes = []

    def respond(request):
        """为输入每项返回独立结果，记录实际网络请求规模。"""
        texts = json.loads(request.content)["input"]
        sizes.append(len(texts))
        return httpx.Response(
            200,
            json={"data": [{"index": index, "embedding": [1, 0]} for index in range(len(texts))]},
        )

    client = EmbeddingClient(Catalog(), "memory-embedding", transport=httpx.MockTransport(respond))
    batch = await client.embed(("事实",) * 129)
    assert len(batch.vectors) == 129
    assert sizes == [128, 1]
    with pytest.raises(EmbeddingError, match="预算"):
        await client.embed(("事实",) * 1025)
    assert sizes == [128, 1]


@pytest.fixture
def index(tmp_path):
    """只建立向量子任务所需的真实数据库表，不依赖全局初始化迁移。"""
    from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore

    path = tmp_path / "vectors.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE chat_tagged_memory (
                id INTEGER PRIMARY KEY,revision INTEGER,status TEXT,is_deleted INTEGER,
                valid_from DATETIME,valid_until DATETIME);
            CREATE TABLE chat_memory_embedding (
                memory_id INTEGER,revision INTEGER,model_key TEXT,vector_json TEXT,
                created_at DATETIME,PRIMARY KEY(memory_id,revision,model_key));
            INSERT INTO chat_tagged_memory VALUES(1,1,'active',0,NULL,NULL);
            INSERT INTO chat_tagged_memory VALUES(2,1,'active',0,NULL,NULL);
            INSERT INTO chat_tagged_memory VALUES(3,2,'active',0,NULL,NULL);
            INSERT INTO chat_tagged_memory VALUES(4,1,'forgotten',1,NULL,NULL);
            INSERT INTO chat_tagged_memory VALUES(5,1,'active',0,NULL,'2000-01-01');
        """)
    store = TaggedMemoryStore(
        DatabaseParams(backend="sqlite", sqlite_path=str(path)), secret="offline-secret"
    )
    return MemoryVectorIndex(store)


def candidate(id, **changes):
    """创建调用方已经授权的候选，所有检索必须保持这些元数据。"""
    return {
        "id": id,
        "revision": 1,
        "content": "喜欢甜食",
        "topic": "饮食",
        "tags": ["习惯"],
        "kind": "preference",
        "importance": 0.5,
        "source_kind": "self_report",
        "last_used_at": None,
        **changes,
    }


def test_index_rejects_stale_forgotten_expired_and_model_mismatch(index):
    """只缓存授权清单中的当前有效版本，不读取另一模型或未授权记忆。"""
    candidates = [candidate(id) for id in range(1, 6)]
    accepted = index.save(candidates, EmbeddingBatch("model-a", ((1.0, 0.0),) * 5))
    assert accepted == {(1, 1), (2, 1)}
    assert index.load([candidate(1)], "model-a") == {(1, 1): (1.0, 0.0)}
    assert index.load(candidates, "model-b") == {}
    assert [row["id"] for row in index.current(candidates)] == [1, 2]


def test_index_deadline_prevents_commit(index):
    """同步向量事务也受请求截止时间约束，不能在请求过期后提交缓存。"""
    with pytest.raises(EmbeddingError, match="超时"):
        index.save(
            [candidate(1)], EmbeddingBatch("model-a", ((1, 0),)), deadline_at=time.monotonic() - 1
        )
    assert index.load([candidate(1)], "model-a") == {}


async def test_hybrid_search_crosses_tags_and_reuses_only_current_model(index):
    """自然语言命中不同措辞，精确数字候选也可从向量不相似项中召回。"""
    calls = []

    def respond(request):
        """记录输入规模，确保缺失向量一次批量请求。"""
        texts = json.loads(request.content)["input"]
        calls.append(texts)
        mapping = {"甜点和413怎么办": [1, 0], "喜欢甜食": [1, 0], "413网关报错解决方案": [0, 1]}
        return httpx.Response(
            200,
            json={
                "data": [{"index": i, "embedding": mapping[text]} for i, text in enumerate(texts)]
            },
        )

    client = EmbeddingClient(Catalog(), "memory-embedding", transport=httpx.MockTransport(respond))
    search = HybridMemorySearch(index, client)
    candidates = [candidate(1), candidate(2, content="413网关报错解决方案", tags=["网关"])]
    result = await search.search("甜点和413怎么办", candidates)
    assert {row["id"] for row in result} == {1, 2}
    assert result[0]["id"] == 1
    assert result[0]["relevance"] == 1.0
    assert result[0]["content"] == "喜欢甜食"
    assert len(calls) == 2 and len(calls[1]) == 2
    await search.search("甜点和413怎么办", candidates)
    assert len(calls) == 3
    client._catalog.snapshot.model.revision = 2
    await search.search("甜点和413怎么办", candidates)
    assert len(calls) == 5


async def test_search_rechecks_revision_after_embedding_and_respects_deadline(index):
    """Embedding 在途修改版本后不能返回旧正文，超时不能继续写入。"""

    async def respond(request):
        """在计算文档向量时模拟另一个事务遗忘该事实。"""
        texts = json.loads(request.content)["input"]
        if texts == ["喜欢甜食"]:
            with index._store._transaction() as cursor:
                cursor.execute("UPDATE chat_tagged_memory SET revision=2 WHERE id=1")
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [1, 0]} for i in range(len(texts))]}
        )

    search = HybridMemorySearch(
        index,
        EmbeddingClient(Catalog(), "memory-embedding", transport=httpx.MockTransport(respond)),
    )
    assert await search.search("甜点", [candidate(1)]) == []
    with pytest.raises(EmbeddingError, match="超时"):
        await search.search("甜点", [candidate(2)], deadline_at=time.monotonic() - 1)


async def test_missing_model_rejects_semantic_search():
    """未配置语义模型时明确拒绝，不使用聊天模型代替。"""
    config = PluginConfig(onebot_v11_access_token="a" * 32, chat_session_secret="b" * 32)
    assert config.chat_memory_embedding_model == ""
    with pytest.raises(EmbeddingError, match="配置"):
        await EmbeddingClient(Catalog(), "").embed(("事实",))


async def test_search_cancellation_is_not_converted_to_failure(index):
    """取消必须继续传播，原请求退出时释放 HTTP 生命周期。"""

    async def respond(_request):
        """模拟请求持有者取消任务。"""
        raise asyncio.CancelledError

    search = HybridMemorySearch(
        index,
        EmbeddingClient(Catalog(), "memory-embedding", transport=httpx.MockTransport(respond)),
    )
    with pytest.raises(asyncio.CancelledError):
        await search.search("甜点", [candidate(1)])


async def test_source_and_recency_rank_only_relevant_facts(index):
    """相关度相同时优先可信来源，低可信度的新近记录不能挤掉更相关事实。"""
    index.save([candidate(1), candidate(2)], EmbeddingBatch("fixed", ((1, 0), (1, 0))))

    class Embedder:
        """固定几何关系以隔离测试来源权重，缓存避免供应商调用。"""

        async def embed(self, _texts):
            """查询与两条事实语义相关性完全相同。"""
            return EmbeddingBatch("fixed", ((1, 0),))

    result = await HybridMemorySearch(index, Embedder()).search(
        "甜点",
        [
            candidate(1, source_kind="legacy_unknown"),
            candidate(2, source_kind="admin_confirmation"),
        ],
    )
    assert [row["id"] for row in result] == [2, 1]


async def test_cached_dimension_mismatch_rejected_without_reembedding(index):
    """同模型缓存维度发生漂移必须明确重建，不能静默覆盖或混算。"""
    index.save([candidate(1)], EmbeddingBatch("fixed", ((1, 0, 0),)))

    class Embedder:
        """只接受查询，意外重新计算文档直接使测试失败。"""

        async def embed(self, texts):
            """供应商同模型返回新维度，检索必须拒绝混算。"""
            assert texts == ("甜点",)
            return EmbeddingBatch("fixed", ((1, 0),))

    with pytest.raises(EmbeddingError, match="维度"):
        await HybridMemorySearch(index, Embedder()).search("甜点", [candidate(1)])


async def test_model_changes_between_query_and_documents_do_not_write(index):
    """查询与文档之间目录变更时不产生无法比较的混合向量。"""
    catalog = Catalog()

    def respond(request):
        """查询响应触发目录修订，下一次请求解析到新模型版本。"""
        texts = json.loads(request.content)["input"]
        if texts == ["甜点"]:
            catalog.snapshot.model.revision += 1
        return httpx.Response(
            200, json={"data": [{"index": i, "embedding": [1, 0]} for i in range(len(texts))]}
        )

    client = EmbeddingClient(catalog, "memory-embedding", transport=httpx.MockTransport(respond))
    with pytest.raises(EmbeddingError, match="版本"):
        await HybridMemorySearch(index, client).search("甜点", [candidate(1)])
    with index._store._transaction() as cursor:
        cursor.execute("SELECT COUNT(*) AS total FROM chat_memory_embedding")
        assert cursor.fetchone()["total"] == 0


async def test_inflight_timeout_does_not_write_partial_vectors(index):
    """在途模型调用超时会中止整个检索，不生成可被误用的部分缓存。"""

    async def respond(_request):
        """等待超过请求剩余时间，仅在错误忽略取消时才会返回。"""
        await asyncio.sleep(0.05)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})

    client = EmbeddingClient(Catalog(), "memory-embedding", transport=httpx.MockTransport(respond))
    with pytest.raises(EmbeddingError, match="超时"):
        await HybridMemorySearch(index, client).search(
            "甜点", [candidate(1)], deadline_at=time.monotonic() + 0.01
        )
    with index._store._transaction() as cursor:
        cursor.execute("SELECT COUNT(*) AS total FROM chat_memory_embedding")
        assert cursor.fetchone()["total"] == 0


async def test_search_returns_at_most_eight_authorized_current_facts(index):
    """作用域候选很多时稳定截取八条，不将未传入的有效事实混入。"""
    with index._store._transaction() as cursor:
        for id in range(6, 15):
            cursor.execute(
                "INSERT INTO chat_tagged_memory VALUES (%s,1,'active',0,NULL,NULL)", (id,)
            )
    candidates = [candidate(id) for id in (1, 2, *range(6, 15))]
    index.save(candidates, EmbeddingBatch("fixed", ((1, 0),) * len(candidates)))

    class Embedder:
        """仅提供查询向量，所有候选已经在缓存中。"""

        async def embed(self, _texts):
            """使用已绑定模型返回固定查询方向。"""
            return EmbeddingBatch("fixed", ((1, 0),))

    result = await HybridMemorySearch(index, Embedder()).search("甜点", candidates)
    assert [row["id"] for row in result] == [1, 2, 6, 7, 8, 9, 10, 11]


async def test_memory_session_limits_semantic_queries_without_blocking_tags(index):
    """单轮最多两次付费语义检索，之后用关键词兜底，标签目录不消耗额度。"""
    from agent_bot.services.tagged_memory import (
        MemoryArguments,
        MemoryIdentity,
        MemoryResult,
        MemorySession,
    )

    class Store:
        """提供明确工具结果以观察会话配额行为，不访问模型或数据库。"""

        def read_candidates(self, _identity, _arguments):
            """测试检索本身无事实，仍应消耗调用额度。"""
            return []

        def semantic_result(self, _identity, _rows):
            """保留真实公共结果契约。"""
            return MemoryResult(status="recalled", mode="hybrid")

        def execute(self, _identity, _arguments, _deadline):
            """标签浏览应仍可继续。"""
            return MemoryResult(status="tags")

    class Search:
        """模拟外部向量计算以测试生产会话的成本边界。"""

        async def search(self, _query, _candidates, deadline_at=None):
            """用空命中保持场景最小。"""
            return []

    session = MemorySession(
        Store(), MemoryIdentity("bot", "user", "session"), semantic_search=Search()
    )
    for _ in range(2):
        result = await session.execute(MemoryArguments(action="recall", query="甜点"))
        assert result.mode == "hybrid"
    fallback = await session.execute(MemoryArguments(action="recall", query="甜点"))
    assert fallback.mode == "lexical"
    assert fallback.fallback_reason == "semantic_budget_exhausted"
    assert (await session.execute(MemoryArguments(action="tags"))).status == "tags"


class BlockingIndex:
    """允许跨线程调用，用事件模拟已开始但尚未结束的数据库事务。"""

    def __init__(self, stage):
        """外部测试控制释放，有限等待保证实现错误时也不会挂死测试。"""
        self.stage = stage
        self.started = threading.Event()
        self.released = threading.Event()
        self.finished = threading.Event()
        self.current_calls = 0

    def _pause(self, stage):
        """只有目标数据库阶段等待，最终完成事件代表事务已结束。"""
        if self.stage == stage:
            self.started.set()
            self.released.wait(timeout=0.5)
            self.finished.set()

    def current(self, candidates):
        """起始与最终有效版本检查均属于数据库阶段。"""
        self.current_calls += 1
        self._pause("initial" if self.current_calls == 1 else "final")
        return candidates

    def load(self, _candidates, _model_key):
        """返回缺失缓存，使保存阶段必然执行。"""
        self._pause("load")
        return {}

    def save(self, candidates, _batch, deadline_at=None):
        """保存也是已开始的事务，调用者取消时必须等待它退出。"""
        self._pause("save")
        return {(row["id"], row["revision"]) for row in candidates}


class FixedEmbedder:
    """提供确定向量以隔离验证数据库的异步所有权，不访问外部模型。"""

    async def embed(self, texts):
        """每个输入返回同一单位方向，不增加其他异步边界。"""
        return EmbeddingBatch("fixed", ((1, 0),) * len(texts))


@pytest.mark.parametrize("stage", ["initial", "load", "save", "final"])
async def test_database_stages_do_not_block_loop_and_cancellation_joins_transaction(stage):
    """所有数据库阶段让出事件循环，取消后等事务完成才释放请求所有权。"""
    index = BlockingIndex(stage)
    task = asyncio.create_task(
        HybridMemorySearch(index, FixedEmbedder()).search("甜点", [candidate(1)])
    )
    try:
        assert await asyncio.to_thread(index.started.wait, 1)
        assert not index.finished.is_set(), "数据库同步等待阻塞了事件循环"
        task.cancel()
        await asyncio.sleep(0.01)
        assert not task.done(), "请求取消后仍有未收拢的数据库事务"
        index.released.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert index.finished.is_set()
    finally:
        index.released.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_deadline_waits_for_already_started_database_stage():
    """总截止时间不能让请求先退出并遗留数据库事务，事务完成后才报告超时。"""
    index = BlockingIndex("load")
    task = asyncio.create_task(
        HybridMemorySearch(index, FixedEmbedder()).search(
            "甜点", [candidate(1)], deadline_at=time.monotonic() + 0.05
        )
    )
    try:
        assert await asyncio.to_thread(index.started.wait, 1)
        await asyncio.sleep(0.07)
        assert not task.done(), "超时必须等待已经开始的数据库事务结束"
        index.released.set()
        with pytest.raises(EmbeddingError, match="超时"):
            await task
        assert index.finished.is_set()
    finally:
        index.released.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize(
    "kind,preferred,other",
    [
        ("preference", "self_report", "tool_result"),
        ("relationship", "user_correction", "admin_confirmation"),
        ("fact", "tool_result", "self_report"),
        ("fact", "admin_confirmation", "self_report"),
    ],
)
async def test_source_support_matches_fact_kind(index, kind, preferred, other):
    """个人偏好关系依据本人陈述，技术事实更重可验证工具结果和管理员确认。"""
    index.save([candidate(1), candidate(2)], EmbeddingBatch("fixed", ((1, 0), (1, 0))))
    result = await HybridMemorySearch(index, FixedEmbedder()).search(
        "甜点",
        [
            candidate(1, kind=kind, source_kind=other),
            candidate(2, kind=kind, source_kind=preferred),
        ],
    )
    assert [row["id"] for row in result] == [2, 1]


def test_recency_has_thirty_day_half_life(monkeypatch):
    """三十天前使用应恰好保留一半权重，不能误用三十天指数时间常数。"""
    from agent_bot.memory import retrieval

    class FrozenDatetime(datetime):
        """固定UTC时钟，避免验证衰减算法时依赖真实测试日期。"""

        @classmethod
        def now(cls, tz=None):
            """按调用者要求返回固定的带时区时间。"""
            return cls(2026, 10, 20, tzinfo=timezone.utc).astimezone(tz)

    monkeypatch.setattr(retrieval, "datetime", FrozenDatetime)
    used = datetime(2026, 10, 20, tzinfo=timezone.utc) - timedelta(days=30)
    assert retrieval._recency(used) == pytest.approx(0.5)


async def test_search_query_obeys_tool_character_limit():
    """底层检索也拒绝超过工具二百字符限制的问题，不能额外扩大供应商输入。"""
    with pytest.raises(EmbeddingError, match="200"):
        await HybridMemorySearch(BlockingIndex(None), FixedEmbedder()).search("问" * 201, [])


async def test_recent_important_but_irrelevant_fact_cannot_enter_ranking(index):
    """相关性门槛在热度和来源加权之前执行，不相关记忆不能靠元信息进入结果。"""
    index.save([candidate(1)], EmbeddingBatch("fixed", ((0, 1),)))
    result = await HybridMemorySearch(index, FixedEmbedder()).search(
        "量子纠缠",
        [
            candidate(
                1,
                importance=1.0,
                source_kind="user_correction",
                last_used_at=datetime.now(timezone.utc),
            ),
        ],
    )
    assert result == []
