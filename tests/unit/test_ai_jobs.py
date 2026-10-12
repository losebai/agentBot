"""AI 定时任务拉取、持久队列交接和投递确认。"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import fakeredis.aioredis
import httpx
import pytest
from pydantic import ValidationError

from agent_bot.config import PluginConfig
from agent_bot.integrations.dispatch_ai import DispatchAiClient
from agent_bot.integrations.onebot.ai_jobs import AiJobPoller
from agent_bot.integrations.redis_queue import RedisConversationQueueStore
from agent_bot.models import AiJobResult, ChatContext
from agent_bot.services.chat.routing import ChatRouteSelector
from agent_bot.services.conversation_queue import ConversationJob, PersistedGeneratedReply


def make_config(**overrides: object) -> PluginConfig:
    """提供必要的离线测试凭据。"""
    return PluginConfig(
        onebot_v11_access_token="test-onebot-access-token-with-enough-entropy",
        dispatch_chat_session_secret="test-session-secret-with-enough-entropy",
        **overrides,
    )


def job_result() -> AiJobResult:
    """构造来自 AI 的完整最终正文和原聊天目标。"""
    return AiJobResult(
        id="a" * 32,
        botId="90001",
        sessionId="session-a",
        senderId="10001",
        scene="group",
        groupId="20001",
        prompt="任务",
        status="SUCCEEDED",
        result="模型最终正文",
        runAt="2026-09-26T02:00:00Z",
    )


def test_job_is_offered_without_a_user_tool_keyword_and_serializes_bot_identity():
    """启用后普通对话可由模型自主选 Job，并携带原机器人身份。"""
    route = ChatRouteSelector().select(
        "你好，明天提醒我开会", available_tools=("job",), web_search_enabled=False,
    )
    assert route.chat_tools == ("job",)
    assert route.use_agent
    context = ChatContext(botId="90001", scene="private", senderId="10001")
    assert context.model_dump(by_alias=True)["botId"] == "90001"
    assert "botId" not in ChatContext(
        scene="private", senderId="10001",
    ).model_dump(by_alias=True)


@asynccontextmanager
async def lease():
    """替代热刷新租约，不访问运行时。"""
    yield


async def test_poller_enqueues_stable_job_without_acknowledging_delivery():
    """重复拉取使用同一队列 ID，持久入队不等于已投递。"""
    client = Mock(pending_jobs=AsyncMock(return_value=(job_result(),)), acknowledge_job=AsyncMock())
    queue = Mock(enqueue=AsyncMock())
    config = make_config(
        dispatch_chat_job_enabled=True,
        dispatch_chat_mcp_enabled=True,
        dispatch_chat_ai_token="t" * 40,
    )
    poller = AiJobPoller(
        config_provider=lambda: config,
        client_provider=lambda: client,
        queue_provider=lambda: queue,
        bot_ids_provider=lambda: ["90001"],
        runtime_lease=lease,
    )
    await poller.run_once()
    await poller.run_once()
    first, second = [call.args[0] for call in queue.enqueue.await_args_list]
    assert first.job_id == second.job_id == "ai-job-" + "a" * 32
    assert first.ai_job_id == "a" * 32
    assert first.ai_job_reply.text == "模型最终正文"
    assert first.mention_target_id == "10001"
    client.acknowledge_job.assert_not_awaited()


async def test_poller_does_not_lose_results_when_queue_is_full():
    """本地队列失败不确认 AI 结果，下轮仍能继续拉取。"""
    client = Mock(pending_jobs=AsyncMock(return_value=(job_result(),)), acknowledge_job=AsyncMock())
    queue = Mock(enqueue=AsyncMock(side_effect=ConnectionError))
    config = make_config(
        dispatch_chat_job_enabled=True,
        dispatch_chat_mcp_enabled=True,
        dispatch_chat_ai_token="t" * 40,
    )
    poller = AiJobPoller(
        config_provider=lambda: config,
        client_provider=lambda: client,
        queue_provider=lambda: queue,
        bot_ids_provider=lambda: ["90001"],
        runtime_lease=lease,
    )
    with pytest.raises(ConnectionError):
        await poller.run_once()
    client.acknowledge_job.assert_not_awaited()


async def test_client_polls_and_acknowledges_with_configured_authentication():
    """轮询接口使用服务凭据，确认仅包含机器人与任务 ID。"""
    paths = []

    async def handler(request):
        """隔离 HTTP 网络并检查请求契约。"""
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer " + "t" * 40
        assert request.url.params["botId"] == "90001"
        data = (
            [job_result().model_dump(by_alias=True, mode="json")]
            if request.method == "GET"
            else None
        )
        return httpx.Response(200, json={"code": 200, "data": data})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        token="t" * 40,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert len(await client.pending_jobs("90001")) == 1
        await client.acknowledge_job("90001", "a" * 32)
    finally:
        await client.aclose()
    assert paths == ["/dispatch-ai/job/results", "/dispatch-ai/job/" + "a" * 32 + "/ack"]


async def test_queue_uses_ai_text_and_acknowledges_only_in_commit(chat_plugin, monkeypatch):
    """已生成的正文复用原投递流程，不再次推理或提前确认。"""
    client = Mock(acknowledge_job=AsyncMock())
    service = Mock(client=client, generate_queued=AsyncMock(), commit_queued=AsyncMock())
    monkeypatch.setattr(chat_plugin, "chat_service", service)
    job = ConversationJob(
        jobId="ai-job-" + "a" * 32,
        requestId="ai-job-" + "a" * 32,
        createdAt=1_800_000_000,
        sessionId="session-a",
        botId="90001",
        scene="group",
        groupId="20001",
        userId="10001",
        prompt="任务",
        aiJobId="a" * 32,
        aiJobReply=PersistedGeneratedReply(text="模型最终正文", requestId="ai-job-" + "a" * 32),
    )
    processor = chat_plugin._ConversationProcessor()
    result = await processor.generate(job)
    assert result.reply.text == "模型最终正文"
    service.generate_queued.assert_not_awaited()
    client.acknowledge_job.assert_not_awaited()
    await processor.commit(job, result)
    client.acknowledge_job.assert_awaited_once_with("90001", "a" * 32)


async def test_redis_deduplicates_repeated_polls_and_preserves_receipt_until_completion():
    """并发轮询、不同拉取时间和进程重建仍只产生一条投递任务。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisConversationQueueStore(
        redis,
        key_prefix="jobs-test",
        group_name="g",
        consumer_name="c",
        max_pending=10,
        result_ttl_seconds=60,
    )
    await store.initialize()
    job = ConversationJob(
        jobId="ai-job-" + "a" * 32,
        requestId="ai-job-" + "a" * 32,
        createdAt=1,
        sessionId="session-a",
        botId="90001",
        scene="private",
        userId="10001",
        prompt="任务",
        aiJobId="a" * 32,
        aiJobReply=PersistedGeneratedReply(text="模型正文", requestId="ai-job-" + "a" * 32),
    )
    try:
        ids = await asyncio.gather(
            store.enqueue(job), store.enqueue(job.model_copy(update={"created_at": 2}))
        )
        assert ids[0] == ids[1]
        assert await redis.xlen("jobs-test:jobs") == 1
        receipt = await redis.hget("jobs-test:receipt-index", ids[0])
        assert await redis.ttl(receipt) == -1
        await store.complete(ids[0])
        assert 0 < await redis.ttl(receipt) <= 60
        assert await store.enqueue(job.model_copy(update={"created_at": 3})) == ids[0]
        assert await redis.xlen("jobs-test:jobs") == 0
    finally:
        await redis.aclose()


@pytest.mark.parametrize(
    "fields",
    [
        {"dispatch_chat_mcp_enabled": False, "dispatch_chat_ai_token": "t" * 40},
        {"dispatch_chat_mcp_enabled": True, "dispatch_chat_ai_token": None},
        {"dispatch_chat_mcp_enabled": True, "dispatch_chat_ai_token": "short"},
    ],
)
def test_job_configuration_requires_authenticated_mcp(fields):
    """未配置必要的服务边界时不能开启有副作用的任务工具。"""
    with pytest.raises(ValidationError):
        make_config(dispatch_chat_job_enabled=True, **fields)


async def test_disabled_poller_does_not_contact_ai():
    """默认关闭时不访问 Job API，保持现有部署行为。"""
    client = Mock(pending_jobs=AsyncMock())
    poller = AiJobPoller(
        config_provider=make_config,
        client_provider=lambda: client,
        queue_provider=Mock,
        bot_ids_provider=lambda: ["90001"],
        runtime_lease=lease,
    )
    await poller.run_once()
    client.pending_jobs.assert_not_awaited()


async def test_poller_advances_past_blocked_results_and_restarts_at_end():
    """被屏蔽的任务不阻塞后续页，遍历结束后重新扫描未确认结果。"""
    first = job_result()
    second = first.model_copy(update={"id": "b" * 32, "sender_id": "10002"})
    client = Mock(pending_jobs=AsyncMock(side_effect=[(first,), (second,), ()]))
    queue = Mock(enqueue=AsyncMock())
    config = make_config(
        dispatch_chat_job_enabled=True,
        dispatch_chat_mcp_enabled=True,
        dispatch_chat_ai_token="t" * 40,
        dispatch_chat_blocked_users={"10001"},
    )
    poller = AiJobPoller(
        config_provider=lambda: config,
        client_provider=lambda: client,
        queue_provider=lambda: queue,
        bot_ids_provider=lambda: ["90001"],
        runtime_lease=lease,
    )
    for _ in range(3):
        await poller.run_once()
    assert [call.args for call in client.pending_jobs.await_args_list] == [
        ("90001", None),
        ("90001", first.id),
        ("90001", second.id),
    ]
    queue.enqueue.assert_awaited_once()
    assert queue.enqueue.await_args.args[0].ai_job_id == second.id
    assert poller._cursors["90001"] is None


async def test_client_rejects_foreign_bot_result():
    """服务返回其他机器人的任务时拒绝投递。"""
    from agent_bot.integrations.dispatch_ai import DispatchAiProtocolError

    response = job_result().model_copy(update={"bot_id": "90002"})
    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        token="t" * 40,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"code": 200, "data": [response.model_dump(by_alias=True)]},
            )
        ),
    )
    try:
        with pytest.raises(DispatchAiProtocolError):
            await client.pending_jobs("90001")
    finally:
        await client.aclose()


async def test_failed_ack_retries_commit_without_repeating_delivery(chat_plugin, monkeypatch):
    """使用真实 Redis 队列恢复流程，确认故障只重试提交，不重发消息或调用模型。"""
    from agent_bot.integrations.dispatch_ai import DispatchAiUnavailableError
    from agent_bot.services.conversation_queue import ConversationQueue, QueuePolicy

    client = Mock(
        acknowledge_job=AsyncMock(side_effect=[DispatchAiUnavailableError("retry"), None])
    )
    service = Mock(client=client, generate_queued=AsyncMock(), commit_queued=AsyncMock())
    monkeypatch.setattr(chat_plugin, "chat_service", service)
    processor = chat_plugin._ConversationProcessor()
    processor.deliver_part = AsyncMock()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisConversationQueueStore(
        redis,
        key_prefix="ack-test",
        group_name="g",
        consumer_name="c",
        max_pending=10,
        result_ttl_seconds=60,
    )
    await store.initialize()
    job = ConversationJob(
        jobId="ai-job-" + "a" * 32,
        requestId="ai-job-" + "a" * 32,
        createdAt=1_800_000_000,
        sessionId="session-a",
        botId="90001",
        scene="private",
        userId="10001",
        prompt="任务",
        aiJobId="a" * 32,
        aiJobReply=PersistedGeneratedReply(text="模型正文", requestId="ai-job-" + "a" * 32),
    )
    try:
        await store.enqueue(job)
        entry = (await store.read_new(count=1, block_ms=1))[0]
        queue = ConversationQueue(
            store=store,
            processor=processor,
            policy=QueuePolicy(1, 2, 0.001, 0.001, 600),
            clock=lambda: 1_800_000_000,
        )
        await queue.process_entry(entry)
        processor.deliver_part.assert_awaited_once()
        assert client.acknowledge_job.await_count == 2
        service.generate_queued.assert_not_awaited()
        assert await store.source_length() == 0
    finally:
        await redis.aclose()
