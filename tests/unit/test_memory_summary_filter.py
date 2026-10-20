"""以真实隔离数据库验证摘要遗忘传播，不依赖提示词或正文关键词。"""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from agent_bot.session_context import SessionContextPolicy, SessionContextStore
from agent_bot.settings import DatabaseParams
from agent_bot.state import MemorySummary


@pytest.fixture(scope="session", autouse=True)
def after_nonebot_init():
    """数据库摘要测试不装配 QQ 插件或执行生产生命周期。"""


@pytest.fixture
def summary_database(tmp_path):
    """创建发布初始化脚本的完整新库，只写入本测试的虚构证据。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "summary.db"))
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text("utf-8"))
        connection.execute(
            "INSERT INTO chat_tagged_memory (id,scope_id,memory_key,content,create_time,"
            "update_time,scope,tags_json,material_ids_json,subject_id,subject_type) "
            "VALUES (1,?,'preference','喜欢茶','2026-10-19','2026-10-19',"
            "'user','[]','[]',?,'user')",
            ("a" * 64, "a" * 64),
        )
        connection.execute(
            "INSERT INTO chat_memory_evidence (memory_id,first_revision,source_kind,"
            "source_actor_id,source_session_id,source_event_id,recorded_at,excerpt,"
            "evidence_fingerprint) VALUES (1,1,'user_statement',?,'session','qq:bot:message',"
            "'2026-10-19','我喜欢茶','fingerprint')", ("a" * 64,),
        )
    return params


def summary_entry(*, memory_refs=False):
    """固定真实来源条目，任何改变身份或事件引用的测试都应被过滤。"""
    return {"kind": "preference", "subject_id": "a" * 64, "content": "喜欢茶",
            "status": "active", "source_kind": "user",
            "source_event_refs": ["qq:bot:message"], "source_request_refs": ["request"],
            "memory_refs": [{"id": 1, "revision": 1}] if memory_refs else []}


@pytest.mark.parametrize("memory_refs", [False, True])
@pytest.mark.parametrize("change", [
    "status='deleted',is_deleted=1", "status='expired'", "status='merged'", "revision=2",
    "valid_until='2026-10-19'", "valid_from='2027-01-01'",
])
def test_inflight_summary_cannot_restore_inactive_or_superseded_sources(
    summary_database, memory_refs, change,
):
    """压缩开始后原事实失效，即使摘要只引用来源事件也必须在提交时移除旧正文。"""
    from agent_bot.memory.summary_filter import MemorySummaryFilter

    store = SessionContextStore(
        summary_database, policy=SessionContextPolicy(0, 10_000, True, 4000),
        summary_filter=MemorySummaryFilter(clock=lambda: datetime(2026, 10, 20)),
    )
    store.record_received("session", "用于压缩的独立事件", request_id="trigger")
    compression = store.finish_request("session", "trigger")
    with sqlite3.connect(summary_database.sqlite_path) as connection:
        # 参数是固定测试枚举，不来自外部请求。
        connection.execute(f"UPDATE chat_tagged_memory SET {change} WHERE id=1")
    assert store.apply_memory(compression, MemorySummary((summary_entry(memory_refs=memory_refs),)))
    assert store.context("session").memory == ""
    with sqlite3.connect(summary_database.sqlite_path) as connection:
        row = connection.execute(
            "SELECT memory_summary,memory_facts_json FROM chat_session_context",
        ).fetchone()
    assert row[0] == ""
    assert json.loads(row[1])["entries"] == []


def test_committed_summary_is_rechecked_after_forgetting(summary_database):
    """提交时仍有效的来源，在之后遗忘也不能继续通过会话摘要出现。"""
    from agent_bot.memory.summary_filter import MemorySummaryFilter

    store = SessionContextStore(
        summary_database, policy=SessionContextPolicy(0, 10_000, True, 4000),
        summary_filter=MemorySummaryFilter(),
    )
    store.record_received("session", "独立事件", request_id="trigger")
    compression = store.finish_request("session", "trigger")
    assert store.apply_memory(compression, MemorySummary((summary_entry(memory_refs=True),)))
    assert "喜欢茶" in store.context("session").memory
    with sqlite3.connect(summary_database.sqlite_path) as connection:
        connection.execute("UPDATE chat_tagged_memory SET is_deleted=1,status='deleted' WHERE id=1")
    assert store.context("session").memory == ""


@pytest.mark.parametrize("mutation", [
    {"subject_id": "b" * 64}, {"source_event_refs": ["foreign"]},
    {"memory_refs": [{"id": 999, "revision": 1}]},
    {"memory_refs": [{"id": 1, "revision": 2}]},
])
def test_memory_reference_requires_same_origin_and_actor(summary_database, mutation):
    """仅有记忆ID不足以授权，主体、真实事件和版本必须全部可证明。"""
    import pymysql

    from agent_bot.database import connect_database
    from agent_bot.memory.summary_filter import MemorySummaryFilter

    connection = connect_database(summary_database, cursorclass=pymysql.cursors.DictCursor)
    try:
        entry = {**summary_entry(memory_refs=True), **mutation}
        assert MemorySummaryFilter()(connection, "session", (entry,)) == ()
        assert MemorySummaryFilter()(connection, "other-session", (summary_entry(
            memory_refs=True,
        ),)) == ()
    finally:
        connection.close()


@pytest.mark.parametrize("entry_path", ["queue", "direct"])
async def test_queued_source_matches_real_memory_evidence_and_forgetting(
    summary_database, monkeypatch, entry_path,
):
    """真人入站、工具保存和摘要共用真实来源，遗忘后端到端停止摘要复述。"""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent_bot.config import RuntimeConfig
    from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
    from agent_bot.integrations.onebot.tooling import ChatToolSessionManager
    from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
    from agent_bot.memory.summary_filter import MemorySummaryFilter
    from agent_bot.model_router import ContentModelRouter
    from agent_bot.services.chat.memory import ChatMemoryCompressor
    from agent_bot.services.chat.service import ChatService
    from agent_bot.services.conversation_queue import ConversationJob
    from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity
    from agent_bot.services.tool_sessions import ToolSessionStore
    from agent_bot.session_delivery import SessionDeliveryStore

    secret = "summary-source-test-secret-32-characters-long"
    config = RuntimeConfig(chat_session_secret=secret, chat_mcp_enabled=True,
                           onebot_v11_access_token="onebot-test-token-32-characters-long")
    memory_store = TaggedMemoryStore(summary_database, secret=secret)
    store = SessionContextStore(
        summary_database, policy=SessionContextPolicy(0, 10_000, True, 4000),
        summary_filter=MemorySummaryFilter(),
    )
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=3)
    tools = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: sessions,
        naraka_service_provider=lambda: None, group_member_refresher=AsyncMock(),
        memory_store_provider=lambda: memory_store,
    )
    monkeypatch.setattr(tools, "queued_bot", lambda _: SimpleNamespace(self_id="90001"))
    service = ChatService(
        client=object(), store=store, session_secret=secret,
        session_delivery=SessionDeliveryStore(lambda: store), system_prompt="",
        max_input_chars=100, cooldown_seconds=0, max_concurrent_requests=1,
        model_router=ContentModelRouter("t", "v", "i", "a"),
    )

    @asynccontextmanager
    async def lease():
        """测试不装配完整运行时，也不连接 QQ 或模型。"""
        yield

    processor = ConversationQueueProcessor(
        config_provider=lambda: config, chat_service_provider=lambda: service,
        runtime_lease=lease, tool_session_provider=tools.open_queued_session,
        bot_provider=lambda _: None, reply_composer=None, reply_gateway=None,
        operation_store_provider=lambda: None, image_submitter=AsyncMock(),
        workflow_service_provider=lambda: None, receive_queued=service.receive_queued,
    )
    job = ConversationJob(job_id="human-memory", request_id="qq-human-memory",
                          created_at=1_792_397_000, session_id="session", bot_id="90001",
                          user_id="10001", scene="private",
                          prompt="引用内容：群友喜欢甜\n当前问题：我喜欢茶",
                          memory_source_text="我喜欢茶",
                          chat_tools=("memory",))
    if entry_path == "queue":
        await processor._receive_original(job)
        source_lease = tools.open_queued_session(job)
    else:
        event = SimpleNamespace(user_id=10001, message_id=111, time=job.created_at,
                                get_plaintext=lambda: job.prompt,
                                original_message=SimpleNamespace(
                                    extract_plain_text=lambda: "我喜欢茶",
                                ))
        source_lease = tools.open_chat_session(SimpleNamespace(self_id="90001"), event,
                                               job.session_id)
    async with source_lease as tool_lease:
        if entry_path == "direct":
            await service.receive_queued(job.session_id, job.prompt, request_id=job.request_id)
        context = await sessions.resolve(tool_lease.token)
        identity = context.memory_session.identity
        with pytest.raises(ValueError, match="当前真实用户消息"):
            await context.memory_session.execute(MemoryArguments(
                action="save", scope="user", key="forged-sweet", content="喜欢甜",
                tags=("偏好",), kind="preference", source_excerpt="群友喜欢甜",
                retention_reason="stable_preference",
            ))
        record = memory_store.execute(identity, MemoryArguments(
            action="save", scope="user", key="tea", content="喜欢茶", tags=("偏好",),
            kind="preference", source_excerpt="我喜欢茶", retention_reason="stable_preference",
        )).records[0]
    compression = store.finish_request("session", job.request_id)

    class Client:
        """模型替身引用真实入站事件，生产压缩器负责规范化来源与时间。"""

        async def complete(self, **options):
            """个人偏好归属于服务器绑定的真实用户匿名身份。"""
            assert "群友喜欢甜" not in options["prompt"]
            return json.dumps({"entries": [{"kind": "preference", "status": "active",
                                           "subject_id": MemoryIdentity(
                                               bot_id=job.bot_id, user_id=job.user_id,
                                               session_id=job.session_id,
                                           ).owner("user", secret),
                                           "content": "喜欢茶",
                                           "source_event_refs": [compression.events[0].id]}]})

    await ChatMemoryCompressor(client=Client(), store=store,
                               model_router=ContentModelRouter("t", "v", "i", "a"),
                               max_tokens=512).compress(compression)
    with sqlite3.connect(summary_database.sqlite_path) as connection:
        evidence = connection.execute(
            "SELECT source_event_id,source_actor_id FROM chat_memory_evidence WHERE memory_id=?",
            (record.id,),
        ).fetchone()
        entry = json.loads(connection.execute(
            "SELECT memory_facts_json FROM chat_session_context",
        ).fetchone()[0])["entries"][0]
    assert entry["source_event_refs"] == [evidence[0]]
    assert entry["subject_id"] == evidence[1]
    assert "喜欢茶" in store.context("session").memory
    memory_store.admin_delete(record.id, expected_revision=record.revision)
    assert store.context("session").memory == ""
