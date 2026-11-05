"""从本地 Job 到 Redis 回执及 QQ 投递确认的离线完整链路。"""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fakeredis.aioredis
import pytest
from tests.jobs.test_integration import config
from tests.jobs.test_store import create, scope

from agent_bot.ai.errors import AiError
from agent_bot.ai.routing import RoutingAiClient
from agent_bot.integrations.onebot.ai_jobs import AiJobPoller
from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.integrations.redis_queue import RedisConversationQueueStore
from agent_bot.integrations.sqlite_queue import SqliteConversationQueueStore
from agent_bot.jobs.service import JobService
from agent_bot.models import LlmRequestOptions
from agent_bot.services.conversation_queue import (
    ConversationDeliveryPausedError,
    ConversationJob,
    ConversationQueue,
    PersistedGeneratedReply,
    QueuePolicy,
)

ROOT = Path(__file__).resolve().parents[2]


@asynccontextmanager
async def lease():
    """该测试聚焦队列，运行时互斥另有真实门闩测试。"""
    yield


async def test_sql_ack_failure_retries_without_regeneration_or_redelivery(job_store, job_clock):
    """SQL 确认暂时失败仍保留 Redis 投递进度，重试只提交确认。"""
    job = await job_store.create(scope(), create())
    job_clock[0] += timedelta(seconds=2)
    ai = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(content="模型最终正文")))
    jobs = JobService(job_store, ai)
    client = RoutingAiClient(
        ai,
        timeout_seconds=30,
        default_options=LlmRequestOptions(),
        mcp_services=(),
        job_service=jobs,
    )
    await client.run_due_jobs()
    original_ack = jobs.acknowledge_job
    attempts = []

    async def acknowledge(bot_id, job_id):
        """第一次数据库不可用，第二次执行真实 Store 条件更新。"""
        attempts.append(job_id)
        if len(attempts) == 1:
            raise AiError("AI_DATABASE_UNAVAILABLE", status_code=503)
        await original_ack(bot_id, job_id)

    jobs.acknowledge_job = acknowledge
    service = SimpleNamespace(client=client, generate_queued=AsyncMock(), commit_queued=AsyncMock())
    settings = config(chat_job_enabled=True, chat_mcp_enabled=True)
    gateway = Mock(send_bot_exact_part=AsyncMock())
    processor = ConversationQueueProcessor(
        config_provider=lambda: settings,
        chat_service_provider=lambda: service,
        runtime_lease=lease,
        tool_session_provider=Mock(),
        bot_provider=lambda _: Mock(),
        reply_composer=Mock(),
        reply_gateway=gateway,
        operation_store_provider=Mock(),
        image_submitter=AsyncMock(),
        workflow_service_provider=lambda: None,
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisConversationQueueStore(
        redis,
        key_prefix="python-jobs",
        group_name="g",
        consumer_name="c",
        max_pending=10,
        result_ttl_seconds=60,
    )
    queue = ConversationQueue(
        store=store, processor=processor, policy=QueuePolicy(1, 3, 0.001, 0.001, 60)
    )
    poller = AiJobPoller(
        config_provider=lambda: settings,
        client_provider=lambda: client,
        queue_provider=lambda: queue,
        bot_ids_provider=lambda: ["10001"],
        runtime_lease=lease,
    )
    try:
        await store.initialize()
        await poller.run_once()
        assert len(await client.pending_jobs("10001")) == 1
        # 重复轮询使用相同稳定标识，Redis 中只有一项。
        poller._cursors.clear()
        await poller.run_once()
        entries = await store.read_new(count=10, block_ms=1)
        assert len(entries) == 1
        await queue.process_entry(entries[0])
        assert attempts == [job.id, job.id]
        gateway.send_bot_exact_part.assert_awaited_once()
        ai.complete.assert_awaited_once()
        service.generate_queued.assert_not_awaited()
        assert await client.pending_jobs("10001") == ()
        assert await redis.xlen("python-jobs:jobs") == 0
    finally:
        await queue.stop()
        await redis.aclose()


@pytest.mark.parametrize("backend", ["sqlite", "redis"])
@pytest.mark.parametrize("pause_reason", ["switch", "offline", "host"])
async def test_pause_and_restart_keeps_partial_delivery_without_expiry(
    tmp_path, monkeypatch, backend, pause_reason,
):
    """暂停保留成功分段；过时任务重启后只发送余下正文，两个队列后端一致。"""
    from agent_bot.integrations import sqlite_queue_database

    schema = ROOT / "sql/agent_bot_queue_init.sqlite.sql"
    monkeypatch.setattr(
        sqlite_queue_database, "queue_schema_sql", lambda: schema.read_text("utf-8"),
    )
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    if backend == "redis":
        store = RedisConversationQueueStore(
            redis, key_prefix="pause", group_name="g", consumer_name="c",
            max_pending=10, result_ttl_seconds=1,
        )
    else:
        store = SqliteConversationQueueStore(
            str(tmp_path / "queue.sqlite3"), key_prefix="pause", max_pending=10,
            result_ttl_seconds=1,
        )
    enabled = config(chat_job_enabled=True, chat_mcp_enabled=True, chat_allow_all=True,
                     chat_chunk_chars=2)
    settings = [enabled]
    offline = [False]
    paused = asyncio.Event()
    service = SimpleNamespace(client=SimpleNamespace(acknowledge_job=AsyncMock()))

    async def send(*args, **kwargs):
        """第一段确认后暂停或断线，模拟重启恢复期间暂时不能投递。"""
        if pause_reason == "switch":
            settings[0] = config()
        elif pause_reason == "host":
            settings[0] = enabled.model_copy(update={"chat_active_actions_paused": True})
        else:
            offline[0] = True

    def connected_bot(bot_id):
        """遵循实际 QQ 宿主契约，未连接时抛出 ConnectionError。"""
        if offline[0]:
            raise ConnectionError("OneBot is not connected")
        return Mock()

    gateway = Mock(send_bot_exact_part=AsyncMock(side_effect=send))
    processor = ConversationQueueProcessor(
        config_provider=lambda: settings[0], chat_service_provider=lambda: service,
        runtime_lease=lease, tool_session_provider=Mock(), bot_provider=connected_bot,
        reply_composer=Mock(), reply_gateway=gateway, operation_store_provider=Mock(),
        image_submitter=AsyncMock(), workflow_service_provider=lambda: None,
    )
    deliver = processor.deliver_part

    async def deliver_and_observe(*args):
        """确认实际投递边界拒绝发送后，再让测试停止任务。"""
        try:
            await deliver(*args)
        except ConversationDeliveryPausedError:
            paused.set()
            raise

    processor.deliver_part = deliver_and_observe
    job = ConversationJob(
        jobId="ai-job-" + "a" * 32, requestId="ai-job-" + "a" * 32,
        createdAt=1, sessionId="session-1", botId="10001", scene="private", userId="10002",
        prompt="内部任务", aiJobId="a" * 32,
        aiJobReply=PersistedGeneratedReply(text="甲乙丙丁", requestId="ai-job-" + "a" * 32),
    )
    queue = ConversationQueue(store=store, processor=processor, policy=QueuePolicy(1, 3, 1, 1, 30))
    task = None
    try:
        await store.initialize()
        await store.enqueue(job)
        entry = (await store.read_new(count=1, block_ms=1))[0]
        task = asyncio.create_task(queue.process_entry(entry))
        await asyncio.wait_for(paused.wait(), 3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        task = None
        state = await store.load_state(entry.stream_id)
        assert state.delivery_index == 1 and state.attempt == 1
        assert await store.dead_letter_length() == 0
        service.client.acknowledge_job.assert_not_awaited()
        # 越过原一秒保留期，暂停中的生成结果仍必须可恢复。
        await asyncio.sleep(1.1)
        assert await store.load_result(entry.stream_id) is not None
        settings[0] = enabled.model_copy(update={"chat_chunk_chars": 1})
        offline[0] = False
        gateway.send_bot_exact_part.side_effect = None
        restarted = ConversationQueue(
            store=store, processor=processor, policy=QueuePolicy(1, 3, 1, 1, 30),
        )
        await restarted.process_entry(entry)
        assert gateway.send_bot_exact_part.await_count == 2
        service.client.acknowledge_job.assert_awaited_once_with("10001", "a" * 32)
        assert await store.load_result(entry.stream_id) is None
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await redis.aclose()
