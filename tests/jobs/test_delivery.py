"""从本地 Job 到 Redis 回执及 QQ 投递确认的离线完整链路。"""

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fakeredis.aioredis
from tests.jobs.test_integration import config
from tests.jobs.test_store import create, scope

from agent_bot.ai.errors import AiError
from agent_bot.ai.routing import RoutingAiClient
from agent_bot.integrations.onebot.ai_jobs import AiJobPoller
from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.integrations.redis_queue import RedisConversationQueueStore
from agent_bot.jobs.service import JobService
from agent_bot.models import LlmRequestOptions
from agent_bot.services.conversation_queue import ConversationQueue, QueuePolicy


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
