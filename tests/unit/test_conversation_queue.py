from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from pydantic import ValidationError
from redis.exceptions import RedisError

from agent_bot.ai.chat_errors import ChatAiRejectedError, ChatAiUnavailableError
from agent_bot.integrations.redis_queue import RedisConversationQueueStore
from agent_bot.models import ChatContext, ChatMediaItem, ChatMediaResponse
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationJobExpiredError,
    ConversationQueue,
    ConversationQueueError,
    ConversationQueueFullError,
    ConversationResult,
    QueuePolicy,
)


def text_result(text: str) -> ConversationResult:
    """构造有模型来源且分段一致的测试结果。"""

    return ConversationResult(
        kind="TEXT", reply=dict(text=text, requestId="req-1"), deliveryParts=(text,),
    )


def text_job(
    *,
    job_id: str = "job-1",
    prompt: str = "你好",
    session_id: str = "qq_session_1",
) -> ConversationJob:
    return ConversationJob(
        jobId=job_id,
        requestId=f"qq-{job_id}",
        createdAt=1_800_000_000,
        sessionId=session_id,
        botId="90001",
        scene="group",
        groupId="20001",
        userId="10001",
        prompt=prompt,
        userName="测试用户",
        chatContext=ChatContext(
            scene="group",
            groupId="20001",
            senderId="10001",
            senderName="测试用户",
        ),
        chatTools=("mention_member",),
        mentionTargetId="10002",
        recentGroupContext="群内最近消息",
    )


def processor_mock() -> AsyncMock:
    """创建带稳定重试分类契约的队列处理器替身。"""

    processor = AsyncMock()
    processor.retryable_errors = (ChatAiUnavailableError, ConnectionError, TimeoutError)
    processor.start_delivery_count.return_value = 1
    return processor


@pytest.mark.parametrize("remaining", [(), ("mention_member",)])
def test_restored_job_removes_retired_reply_tool(remaining) -> None:
    """已入队的旧任务仍可恢复，但不能再次把发送工具传给 Agent。"""
    payload = text_job().model_dump(by_alias=True)
    payload["chatTools"] = ["send_reply", *remaining]
    restored = ConversationJob.model_validate_json(json.dumps(payload))
    assert restored.chat_tools == remaining
    assert restored.prompt == "你好"


@pytest.fixture
async def queue_store():
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    store = RedisConversationQueueStore(
        redis,
        key_prefix="test:chat",
        group_name="workers",
        consumer_name="consumer-1",
        max_pending=2,
        result_ttl_seconds=60,
    )
    await store.initialize()
    try:
        yield store
    finally:
        await redis.aclose()


def test_conversation_job_round_trips_without_runtime_credentials() -> None:
    job = text_job()

    restored = ConversationJob.model_validate_json(job.model_dump_json(by_alias=True))

    assert restored == job
    payload = restored.model_dump(by_alias=True)
    assert payload["chatTools"] == ("mention_member",)
    assert "toolSessionToken" not in payload
    assert "token" not in job.model_dump_json(by_alias=True).casefold()


@pytest.mark.parametrize("parts", [("abc", "wrong"), ("", "abcdef"), ()])
def test_workflow_start_parts_must_equal_proven_reply(parts) -> None:
    """Reject invalid persisted parts instead of sending text outside the proven reply."""

    payload = text_job().model_dump(by_alias=True)
    payload.update(
        chatTools=("local_workflow_generate_image",),
        imageStartReply={"text": "abcdef", "requestId": "start-request", "source": "LLM"},
        imageStartParts=parts,
    )
    with pytest.raises(ValidationError, match="start parts must equal"):
        ConversationJob.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_workflow_start_parts_resume_durably_after_hot_config_change(
    queue_store, monkeypatch, chat_plugin, restart,
) -> None:
    """After abc succeeds, retries/restarts send only the persisted def despite chunk=2."""

    monkeypatch.setattr(chat_plugin.plugin_config, "chat_chunk_chars", 3)
    payload = text_job().model_dump(by_alias=True)
    payload.update(
        chatTools=("local_workflow_generate_image",),
        imageStartReply={"text": "abcdef", "requestId": "start-request", "source": "LLM"},
        imageStartParts=("abc", "def"),
    )
    await queue_store.enqueue(ConversationJob.model_validate(payload))
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    assert entry.job.image_start_parts == ("abc", "def")
    bot = AsyncMock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    attempts: list[str] = []

    async def send_group_msg(**kwargs) -> None:
        """Fail the second part once after checking the durable first-part checkpoint."""

        attempts.append(str(kwargs["message"]))
        if len(attempts) == 2:
            state = await queue_store.load_state(entry.stream_id)
            assert state.image_start_delivery_index == 1
            assert state.image_start_delivered is False
            monkeypatch.setattr(chat_plugin.plugin_config, "chat_chunk_chars", 2)
            if restart:
                raise asyncio.CancelledError
            raise ConnectionError("onebot offline")

    bot.send_group_msg.side_effect = send_group_msg
    processor = chat_plugin._ConversationProcessor()
    monkeypatch.setattr(processor, "generate", AsyncMock(return_value=text_result("done")))
    monkeypatch.setattr(processor, "deliver_part", AsyncMock())
    monkeypatch.setattr(processor, "commit", AsyncMock())
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(
            workers=1, max_attempts=2, retry_initial_seconds=0.01,
            retry_max_seconds=0.01, job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )

    if restart:
        with pytest.raises(asyncio.CancelledError):
            await queue.process_entry(entry)
        restored = ConversationJob.model_validate_json(entry.job.model_dump_json(by_alias=True))
        assert restored.image_start_parts == ("abc", "def")
    await queue.process_entry(entry)

    assert attempts == ["abc", "def", "def"]
    processor.generate.assert_awaited_once()
    assert await queue_store.source_length() == 0


@pytest.mark.asyncio
async def test_start_index_survives_all_other_redis_state_updates(queue_store) -> None:
    """Result, retry and normal delivery checkpoints must preserve start-part progress."""

    stream_id = await queue_store.enqueue(text_job())
    await queue_store.save_image_start_delivery_index(stream_id, 1)
    await queue_store.save_retry(stream_id, attempt=2, available_at=1_800_000_010)
    await queue_store.save_result(stream_id, text_result("done"))
    await queue_store.save_delivery_index(stream_id, 1)
    await queue_store.save_image_start_delivered(stream_id)

    state = await queue_store.load_state(stream_id)
    assert state.image_start_delivery_index == 1
    assert state.image_start_delivered is True
    assert state.result_ready is True
    assert state.delivery_index == 1
    assert state.attempt == 2


def test_conversation_job_accepts_all_enabled_chat_tools() -> None:
    payload = text_job().model_dump(by_alias=True)
    payload["chatTools"] = (
        "naraka_query",
        "naraka_search",
        "mention_member",
        "local_workflow_list",
        "local_workflow_generate_image",
    )

    job = ConversationJob.model_validate(payload)

    assert job.chat_tools == payload["chatTools"]


def test_conversation_job_rejects_invalid_delivery_target() -> None:
    with pytest.raises(ValueError, match="group"):
        ConversationJob(
            jobId="job-invalid",
            requestId="qq-job-invalid",
            createdAt=1_800_000_000,
            sessionId="qq_session_1",
            botId="90001",
            scene="group",
            userId="10001",
            prompt="你好",
        )


@pytest.mark.asyncio
async def test_redis_store_enforces_capacity_and_cleans_completed_entry(queue_store) -> None:
    """Verify redis store enforces capacity and cleans completed entry."""

    first_id = await queue_store.enqueue(text_job())
    second_id = await queue_store.enqueue(text_job(job_id="job-2"))

    with pytest.raises(ConversationQueueFullError):
        await queue_store.enqueue(text_job(job_id="job-3"))

    entries = await queue_store.read_new(count=2, block_ms=1)
    assert [entry.stream_id for entry in entries] == [first_id, second_id]
    assert entries[0].job == text_job()

    result = text_result("回答")
    await queue_store.save_result(first_id, result)
    await queue_store.save_retry(first_id, attempt=2, available_at=1_800_000_010)
    await queue_store.save_image_start_delivered(first_id)
    await queue_store.save_delivery_index(first_id, 1)

    assert await queue_store.load_result(first_id) == result
    state = await queue_store.load_state(first_id)
    assert state.attempt == 2
    assert state.available_at == 1_800_000_010
    assert state.delivery_index == 1
    assert state.image_start_delivered is True

    await queue_store.complete(first_id)

    assert await queue_store.load_result(first_id) is None
    assert await queue_store.source_length() == 1


@pytest.mark.asyncio
async def test_redis_store_moves_failed_entry_to_bounded_dead_letter(queue_store) -> None:
    """Verify redis store moves failed entry to bounded dead letter."""

    stream_id = await queue_store.enqueue(text_job())
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    await queue_store.save_result(stream_id, text_result("回答"))

    await queue_store.dead_letter(entry, reason="ChatAiRejectedError", attempt=1)

    assert await queue_store.source_length() == 0
    assert await queue_store.dead_letter_length() == 1
    assert await queue_store.load_result(stream_id) is None


@pytest.mark.asyncio
async def test_new_consumer_reclaims_unacknowledged_entries_from_old_consumer(
    queue_store,
) -> None:
    await queue_store.enqueue(text_job())
    old_entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    replacement = RedisConversationQueueStore(
        queue_store._redis,
        key_prefix="test:chat",
        group_name="workers",
        consumer_name="consumer-2",
        max_pending=2,
        result_ttl_seconds=60,
    )
    await replacement.initialize()
    # fakeredis uses integer wall-clock milliseconds; Windows can expose a coarser clock tick.
    await asyncio.sleep(0.02)

    reclaimed = await replacement.reclaim(count=10, min_idle_ms=0)

    assert reclaimed == [old_entry]
    await replacement.complete(old_entry.stream_id)


@pytest.mark.asyncio
async def test_delivery_retry_reuses_persisted_generation_result(queue_store) -> None:
    """Verify delivery retry reuses persisted generation result."""

    await queue_store.enqueue(text_job())
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    processor = processor_mock()
    processor.generate.return_value = text_result("只生成一次")
    processor.delivery_count.return_value = 1
    processor.deliver_part.side_effect = [ConnectionError("onebot unavailable"), None]
    queue = ConversationQueue(
        store=queue_store,
        processor=processor,
        policy=QueuePolicy(
            workers=1,
            max_attempts=3,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
            job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )

    await queue.process_entry(entry)

    processor.generate.assert_awaited_once_with(entry.job)
    assert processor.deliver_part.await_count == 2
    processor.deliver_failure.assert_not_awaited()
    processor.commit.assert_awaited_once()
    assert await queue_store.source_length() == 0


@pytest.mark.asyncio
async def test_silent_result_completes_without_delivery_retry_or_failure(queue_store) -> None:
    """零条消息是模型的合法选择，应正常确认任务而不是进入死信或失败回复。"""
    await queue_store.enqueue(text_job())
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    processor = processor_mock()
    processor.start_delivery_count.return_value = 0
    processor.generate.return_value = ConversationResult(kind="SILENT")
    processor.delivery_count.return_value = 0
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(
            workers=1, max_attempts=3, retry_initial_seconds=0.01,
            retry_max_seconds=0.01, job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )
    await queue.process_entry(entry)
    processor.generate.assert_awaited_once()
    processor.deliver_part.assert_not_awaited()
    processor.deliver_failure.assert_not_awaited()
    processor.commit.assert_awaited_once()
    assert await queue_store.source_length() == 0


@pytest.mark.asyncio
async def test_workflow_start_message_is_delivered_once_before_generation(
    queue_store,
) -> None:
    """开画提示投递进度持久化后，生成重试不能重复发送提示。"""

    job = text_job(job_id="job-workflow-start")
    payload = job.model_dump(by_alias=True)
    payload["chatTools"] = (
        "local_workflow_list",
        "local_workflow_generate_image",
    )
    payload["imageStartReply"] = {"text": "好啦，我开始画了。", "requestId": job.request_id}
    workflow_job = ConversationJob.model_validate(payload)
    await queue_store.enqueue(workflow_job)
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    order: list[str] = []
    processor = processor_mock()

    async def deliver_start(_job: ConversationJob, _index: int) -> None:
        """记录开画提示在生成之前发送。"""

        order.append("start")

    attempts = 0

    async def generate(_job: ConversationJob) -> ConversationResult:
        """第一次生成失败后返回有来源的结果。"""

        nonlocal attempts
        attempts += 1
        order.append(f"generate-{attempts}")
        if attempts == 1:
            raise ChatAiUnavailableError("temporary")
        return text_result("完成")

    processor.deliver_start_part.side_effect = deliver_start
    processor.generate.side_effect = generate
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store,
        processor=processor,
        policy=QueuePolicy(
            workers=1,
            max_attempts=2,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
            job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )

    await queue.process_entry(entry)

    assert order == ["start", "generate-1", "generate-2"]
    processor.deliver_start_part.assert_awaited_once_with(entry.job, 0)
    assert await queue_store.source_length() == 0


@pytest.mark.asyncio
async def test_transient_model_failure_retries_but_permanent_failure_goes_dead_letter(
    queue_store,
) -> None:
    """Verify transient model failure retries but permanent failure goes dead letter."""

    transient_id = await queue_store.enqueue(text_job(job_id="job-transient"))
    transient_entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    transient_processor = processor_mock()
    transient_processor.generate.side_effect = [
        ChatAiUnavailableError("temporary"),
        text_result("恢复成功"),
    ]
    transient_processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store,
        processor=transient_processor,
        policy=QueuePolicy(
            workers=1,
            max_attempts=2,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
            job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )

    await queue.process_entry(transient_entry)

    assert transient_processor.generate.await_count == 2
    transient_processor.deliver_failure.assert_not_awaited()
    assert await queue_store.load_result(transient_id) is None

    permanent_id = await queue_store.enqueue(text_job(job_id="job-permanent"))
    permanent_entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    permanent_processor = processor_mock()
    permanent_error = ChatAiRejectedError("rejected")
    permanent_processor.generate.side_effect = permanent_error
    permanent_queue = ConversationQueue(
        store=queue_store,
        processor=permanent_processor,
        policy=queue.policy,
        clock=lambda: 1_800_000_000,
    )

    await permanent_queue.process_entry(permanent_entry)

    permanent_processor.deliver_part.assert_not_awaited()
    permanent_processor.deliver_failure.assert_awaited_once_with(
        permanent_entry.job,
        permanent_error,
    )
    assert await queue_store.load_result(permanent_id) is None
    assert await queue_store.dead_letter_length() == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["generate", "commit"])
async def test_memory_disconnect_retries_without_duplicate_delivery(
    queue_store, caplog, stage: str,
) -> None:
    """记忆断连能恢复，投递后的提交重试不能重跑模型或重复发送正文。"""
    from pymysql.err import OperationalError

    from agent_bot.memory_store import ChatMemoryStoreError

    failure = ChatMemoryStoreError("memory unavailable")
    failure.__cause__ = OperationalError(2013, "private-connection-details")
    await queue_store.enqueue(text_job())
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    processor = processor_mock()
    processor.generate.return_value = text_result("恢复成功")
    processor.delivery_count.return_value = 1
    if stage == "generate":
        processor.generate.side_effect = [failure, text_result("恢复成功")]
    else:
        processor.commit.side_effect = [failure, None]
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(
            workers=1, max_attempts=2, retry_initial_seconds=0.01,
            retry_max_seconds=0.01, job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )

    await queue.process_entry(entry)

    assert await queue_store.dead_letter_length() == 0
    assert await queue_store.source_length() == 0
    assert processor.generate.await_count == (2 if stage == "generate" else 1)
    assert processor.commit.await_count == (2 if stage == "commit" else 1)
    processor.deliver_part.assert_awaited_once()
    processor.deliver_failure.assert_not_awaited()
    assert "2013" in caplog.text
    assert "private-connection-details" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("code, attempts", [(2006, 2), (2013, 2), (1045, 1), (1146, 1), (None, 1)])
async def test_memory_retry_budget_and_permanent_errors(queue_store, code, attempts) -> None:
    """持续断连受次数上限约束；权限、缺表和损坏数据不能反复执行。"""
    from pymysql.err import OperationalError

    from agent_bot.memory_store import ChatMemoryStoreError

    failure = ChatMemoryStoreError("memory unavailable")
    if code is not None:
        failure.__cause__ = OperationalError(code, "private-connection-details")
    await queue_store.enqueue(text_job())
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    processor = processor_mock()
    processor.generate.side_effect = failure
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(
            workers=1, max_attempts=2, retry_initial_seconds=0.01,
            retry_max_seconds=0.01, job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )

    await queue.process_entry(entry)

    assert processor.generate.await_count == attempts
    processor.deliver_part.assert_not_awaited()
    assert await queue_store.dead_letter_length() == 1


@pytest.mark.asyncio
async def test_memory_pool_timeout_is_retryable_without_matching_error_text(queue_store) -> None:
    """共享池短暂无空闲容量时按队列预算重试，而不是按异常字符串判断。"""
    from pymysql.err import OperationalError

    from _fake_mysql import FakeMySQL
    from agent_bot.database_pool import MySqlConnectionPool
    from agent_bot.memory_store import ChatMemoryStoreError
    from agent_bot.settings import DatabaseParams

    pool = MySqlConnectionPool(
        DatabaseParams("localhost", 3306, "test", "test", "test"),
        max_size=1, acquire_timeout_seconds=0.001,
        connect=FakeMySQL().connect,
    )
    lease = pool.connect()
    with pytest.raises(OperationalError) as caught:
        pool.connect()
    lease.close()
    failure = ChatMemoryStoreError("memory unavailable")
    failure.__cause__ = caught.value
    await queue_store.enqueue(text_job())
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    processor = processor_mock()
    processor.generate.side_effect = [failure, text_result("恢复成功")]
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(
            workers=1, max_attempts=2, retry_initial_seconds=0.01,
            retry_max_seconds=0.01, job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )

    await queue.process_entry(entry)

    assert await queue_store.dead_letter_length() == 0
    processor.deliver_part.assert_awaited_once()
    pool.close()


@pytest.mark.asyncio
async def test_transient_redis_failure_keeps_job_for_retry(queue_store) -> None:
    """Verify transient redis failure keeps job for retry."""

    await queue_store.enqueue(text_job(job_id="job-redis-retry"))
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    processor = processor_mock()
    processor.generate.side_effect = [
        RedisError("external redis temporarily unavailable"),
        text_result("恢复成功"),
    ]
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store,
        processor=processor,
        policy=QueuePolicy(
            workers=1,
            max_attempts=2,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
            job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )

    await queue.process_entry(entry)

    assert processor.generate.await_count == 2
    assert await queue_store.source_length() == 0
    assert await queue_store.dead_letter_length() == 0


@pytest.mark.asyncio
async def test_exhausted_transient_failure_notifies_once_after_final_attempt(
    queue_store,
) -> None:
    await queue_store.enqueue(text_job(job_id="job-exhausted"))
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    error = ChatAiUnavailableError("temporary supplier detail")
    processor = processor_mock()
    processor.generate.side_effect = error
    queue = ConversationQueue(
        store=queue_store,
        processor=processor,
        policy=QueuePolicy(
            workers=1,
            max_attempts=2,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
            job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )

    await queue.process_entry(entry)

    assert processor.generate.await_count == 2
    processor.deliver_failure.assert_awaited_once_with(entry.job, error)
    assert await queue_store.dead_letter_length() == 1


@pytest.mark.asyncio
async def test_expired_job_does_not_start_generation_or_failure_delivery(queue_store) -> None:
    """过期任务仅清理存储，不能开始新的模型或平台调用。"""
    await queue_store.enqueue(text_job(job_id="job-expired"))
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    processor = processor_mock()
    queue = ConversationQueue(
        store=queue_store,
        processor=processor,
        policy=QueuePolicy(
            workers=1,
            max_attempts=2,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
            job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_601,
    )

    await queue.process_entry(entry)

    processor.generate.assert_not_awaited()
    processor.deliver_failure.assert_not_awaited()
    dead = await queue_store._redis.xrange(queue_store._dead_letter_key)
    assert dead[0][1]["reason"] == ConversationJobExpiredError.__name__
    assert await queue_store.dead_letter_length() == 1


@pytest.mark.asyncio
async def test_missing_cached_result_never_regenerates_after_delivery_started(
    queue_store,
) -> None:
    """Verify missing cached result never regenerates after delivery started."""

    stream_id = await queue_store.enqueue(text_job(job_id="job-missing-result"))
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    await queue_store.save_result(
        stream_id,
        text_result("已经生成"),
    )
    await queue_store._redis.delete(queue_store._result_key(stream_id))
    processor = processor_mock()
    queue = ConversationQueue(
        store=queue_store,
        processor=processor,
        policy=QueuePolicy(
            workers=1,
            max_attempts=3,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
            job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )

    await queue.process_entry(entry)

    processor.generate.assert_not_awaited()
    processor.deliver_failure.assert_not_awaited()
    assert await queue_store.source_length() == 0
    assert await queue_store.dead_letter_length() == 1


def test_media_result_keeps_validated_base64_for_delivery_retry() -> None:
    response = ChatMediaResponse(
        text="图片完成",
        items=(ChatMediaItem(type="IMAGE", mimeType="image/png", data="iVBORw=="),),
    )

    result = ConversationResult(kind="MEDIA", media=response)

    assert ConversationResult.model_validate_json(result.model_dump_json()) == result


def test_conversation_result_round_trips_single_mixed_workflow_image() -> None:
    """Verify conversation result round trips single mixed workflow image."""

    result = ConversationResult(
        kind="MIXED",
        reply=dict(text="图片已经生成。", requestId="req-1"),
        deliveryParts=("图片已经生成。",),
        workflowImages=(
            {"mimeType": "image/png", "data": "iVBORw0KGgppbWFnZS0x"},
        ),
    )

    restored = ConversationResult.model_validate_json(result.model_dump_json(by_alias=True))

    assert restored == result


def test_conversation_result_rejects_legacy_multi_image_cache() -> None:
    """旧版本没有 LLM 来源的缓存必须拒绝。"""

    legacy_payload = {
        "kind": "MIXED",
        "text": "旧任务完成。",
        "deliveryParts": ["旧任务完成。"],
        "workflowImages": [
            {"mimeType": "image/png", "data": "iVBORw0KGgppbWFnZS0x"},
            {"mimeType": "image/png", "data": "iVBORw0KGgppbWFnZS0y"},
        ],
    }

    with pytest.raises(ValidationError):
        ConversationResult.model_validate(legacy_payload)


def test_queue_v2_rejects_unproven_text_and_v1_jobs() -> None:
    """无来源证明的文本和显式旧版任务均不能进入 v2 队列。"""

    with pytest.raises(ValidationError):
        ConversationResult.model_validate({"kind": "TEXT", "text": "fixed"})
    payload = text_job().model_dump(by_alias=True)
    payload["version"] = 1
    with pytest.raises(ValidationError):
        ConversationJob.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("malformed", [False, True])
async def test_redis_rejects_old_or_malformed_job_without_processing(
    queue_store, malformed: bool,
) -> None:
    """Redis 解析失败必须确认原条目并隔离死信，不暴露给处理器。"""

    payload = text_job().model_dump(mode="json", by_alias=True)
    payload["version"] = 1
    fields = {"unexpected": "value"} if malformed else {"payload": json.dumps(payload)}
    await queue_store._redis.xadd(queue_store._stream_key, fields)
    entries = await queue_store.read_new(count=2, block_ms=1)
    assert entries == []
    assert await queue_store.source_length() == 0
    assert await queue_store.dead_letter_length() == 1
    pending = await queue_store._redis.xpending(queue_store._stream_key, "workers")
    assert pending["pending"] == 0


@pytest.mark.asyncio
async def test_redis_invalid_result_is_dead_lettered_without_notification(
    queue_store, monkeypatch, chat_plugin,
) -> None:
    """缓存旧文本不能触发生成、失败提示或 OneBot 投递。"""

    stream_id = await queue_store.enqueue(text_job())
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    await queue_store._redis.set(
        queue_store._result_key(stream_id),
        json.dumps({"version": 1, "kind": "TEXT", "text": "unproven"}),
    )
    bot = AsyncMock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    generate = AsyncMock()
    compose = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", generate)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", compose)
    processor = chat_plugin._ConversationProcessor()
    failure = AsyncMock(wraps=processor.deliver_failure)
    monkeypatch.setattr(processor, "deliver_failure", failure)
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(1, 2, 0.01, 0.01, 600),
        clock=lambda: 1_800_000_000,
    )
    await queue.process_entry(entry)
    generate.assert_not_awaited()
    compose.assert_not_awaited()
    failure.assert_not_awaited()
    assert not bot.mock_calls
    assert await queue_store.source_length() == 0
    assert await queue_store.dead_letter_length() == 1


@pytest.mark.asyncio
async def test_same_session_entries_wait_for_previous_commit_and_ack(
    queue_store,
) -> None:
    """后续同会话生成必须等前一条提交及 Redis 确认完成。"""

    concurrent_store = RedisConversationQueueStore(
        queue_store._redis,
        key_prefix="test:same-session-concurrent",
        group_name="workers",
        consumer_name="consumer-1",
        max_pending=2,
        result_ttl_seconds=60,
    )
    await concurrent_store.initialize()
    for job in (
        text_job(job_id="job-a1", prompt="a1", session_id="session-a"),
        text_job(job_id="job-a2", prompt="a2", session_id="session-a"),
    ):
        await concurrent_store.enqueue(job)
    entries = await concurrent_store.read_new(count=2, block_ms=1)
    active = 0
    max_active = 0
    first_entry_entered = asyncio.Event()
    release = asyncio.Event()

    async def generate(job):
        """记录同时生成的任务数量并等待测试释放。"""

        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        first_entry_entered.set()
        await release.wait()
        active -= 1
        return text_result(f"answer-{job.prompt}")

    processor = processor_mock()
    processor.generate.side_effect = generate
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=concurrent_store,
        processor=processor,
        policy=QueuePolicy(
            workers=2,
            max_attempts=2,
            retry_initial_seconds=0.01,
            retry_max_seconds=0.01,
            job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )
    for entry in entries:
        queue._schedule(entry)

    try:
        await asyncio.wait_for(first_entry_entered.wait(), timeout=1)
        await asyncio.sleep(0.02)
        assert max_active == 1
        assert processor.generate.await_count == 1
    finally:
        release.set()
        await queue.stop()


@pytest.mark.asyncio
async def test_retry_backoff_does_not_occupy_global_worker(queue_store) -> None:
    """Verify retry backoff does not occupy global worker."""

    retry_store = RedisConversationQueueStore(
        queue_store._redis,
        key_prefix="test:retry-slot",
        group_name="workers",
        consumer_name="consumer-1",
        max_pending=2,
        result_ttl_seconds=60,
    )
    await retry_store.initialize()
    for job in (
        text_job(job_id="job-retry", prompt="retry", session_id="session-retry"),
        text_job(job_id="job-ready", prompt="ready", session_id="session-ready"),
    ):
        await retry_store.enqueue(job)
    entries = await retry_store.read_new(count=2, block_ms=1)
    ready_generated = asyncio.Event()
    retry_calls = 0

    async def generate(job):
        """模拟单个任务重试，记录其他任务仍可生成。"""

        nonlocal retry_calls
        if job.prompt == "retry":
            retry_calls += 1
            if retry_calls == 1:
                raise ChatAiUnavailableError("temporary")
        else:
            ready_generated.set()
        return text_result(f"answer-{job.prompt}")

    processor = processor_mock()
    processor.generate.side_effect = generate
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=retry_store,
        processor=processor,
        policy=QueuePolicy(
            workers=1,
            max_attempts=2,
            retry_initial_seconds=0.2,
            retry_max_seconds=0.2,
            job_ttl_seconds=600,
        ),
        clock=lambda: 1_800_000_000,
    )
    for entry in entries:
        queue._schedule(entry)

    try:
        await asyncio.wait_for(ready_generated.wait(), timeout=0.1)
    finally:
        await queue.stop()


@pytest.mark.parametrize("parts", [("另一段",), ("完整", "", "回复"), ()])
def test_text_parts_must_equal_generated_reply(parts: tuple[str, ...]) -> None:
    """分段不可替换来源文字或添加空分段。"""

    with pytest.raises(ValidationError):
        ConversationResult(
            kind="TEXT", reply=dict(text="完整回复", requestId="req-1"),
            deliveryParts=parts,
        )


def test_queue_v2_accepts_and_roundtrips_provenance() -> None:
    """合法来源与完全一致的分段必须可持久化恢复。"""

    result = text_result("模型回复")
    restored = ConversationResult.model_validate_json(result.model_dump_json(by_alias=True))
    assert restored.version == 2
    assert restored.reply.request_id == "req-1"
    assert restored.reply.source == "LLM"
    assert restored.reply.text == "模型回复"


@pytest.mark.parametrize("kind", ["MEDIA", "WORKFLOW_IMAGES"])
def test_nontext_result_rejects_reply_provenance(kind: str) -> None:
    """纯媒体结果不能夹带可投递文字。"""

    payload = {
        "kind": kind,
        "reply": {"text": "固定", "requestId": "req-1"},
        "media": {"items": [{"type": "IMAGE", "mimeType": "image/png", "data": "iVBORw=="}]}
        if kind == "MEDIA" else None,
        "workflowImages": [{"mimeType": "image/png", "data": "iVBORw0KGgppbWFnZQ=="}]
        if kind == "WORKFLOW_IMAGES" else [],
    }
    with pytest.raises(ValidationError, match="result fields are invalid"):
        ConversationResult.model_validate(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_session", [False, True])
async def test_waiting_job_expiry_prevents_model_and_delivery(
    queue_store, same_session: bool,
) -> None:
    """等待执行槽或会话锁后必须重新校验绝对截止时间。"""

    now = [1_800_000_000.0]
    for job in (
        text_job(job_id="blocking", session_id="busy"),
        text_job(job_id="expired", session_id="busy" if same_session else "other"),
    ):
        await queue_store.enqueue(job)
    first, second = await queue_store.read_new(count=2, block_ms=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    generated: list[str] = []

    async def generate(job):
        """保留首个执行槽，直到测试推进墙上时钟。"""

        generated.append(job.job_id)
        entered.set()
        await release.wait()
        return text_result("done")

    processor = processor_mock()
    processor.generate.side_effect = generate
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(1, 2, 0.01, 0.01, 1), clock=lambda: now[0],
    )
    first_task = asyncio.create_task(queue.process_entry(first))
    await asyncio.wait_for(entered.wait(), 1)
    second_task = asyncio.create_task(queue.process_entry(second))
    await asyncio.sleep(0)
    now[0] += 2
    release.set()
    await asyncio.wait_for(asyncio.gather(first_task, second_task), 1)

    assert generated == ["blocking"]
    processor.deliver_part.assert_not_awaited()
    processor.deliver_failure.assert_not_awaited()
    assert await queue_store.dead_letter_length() == 2


@pytest.mark.asyncio
async def test_running_deadline_cancels_generation_and_releases_session(queue_store) -> None:
    """整条任务超时必须取消正在运行的生成，且不消耗重试次数重新生成。"""

    await queue_store.enqueue(text_job())
    entry = (await queue_store.read_new(count=1, block_ms=1))[0]
    cancelled = asyncio.Event()

    async def generate(_job):
        """持续运行，记录总截止时间触发的取消。"""

        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    processor = processor_mock()
    processor.generate.side_effect = generate
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(1, 3, 0.01, 0.01, 0.04), clock=lambda: 1_800_000_000,
    )
    task = asyncio.create_task(queue.process_entry(entry))
    try:
        await asyncio.wait_for(asyncio.shield(task), 0.3)
    except asyncio.TimeoutError:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        pytest.fail("task deadline did not cancel generation")
    assert cancelled.is_set()
    processor.generate.assert_awaited_once()
    processor.deliver_part.assert_not_awaited()
    processor.deliver_failure.assert_not_awaited()
    assert await queue_store.dead_letter_length() == 1
    async with queue.session_guard(entry.job.session_id):
        pass


@pytest.mark.asyncio
async def test_same_session_retry_does_not_allow_later_job_to_overtake(queue_store) -> None:
    """首条重试退避期间保留会话顺序，并等待确认后才启动第二条。"""

    for job_id in ("first", "second"):
        await queue_store.enqueue(text_job(job_id=job_id))
    entries = await queue_store.read_new(count=2, block_ms=1)
    events: list[str] = []
    processor = processor_mock()

    async def generate(job):
        """首条第一次生成失败，使错误实现暴露乱序。"""

        events.append(f"generate:{job.job_id}")
        if events == ["generate:first"]:
            raise ConnectionError("retry")
        return text_result("done")

    async def commit(job, _result):
        """记录提交先于下一条生成的顺序。"""

        events.append(f"commit:{job.job_id}")

    processor.generate.side_effect = generate
    processor.commit.side_effect = commit
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(2, 2, 0.02, 0.02, 600), clock=lambda: 1_800_000_000,
    )
    await asyncio.gather(*(queue.process_entry(entry) for entry in entries))
    assert events == [
        "generate:first", "generate:first", "commit:first", "generate:second", "commit:second",
    ]
    assert await queue_store.source_length() == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("media_mode", ["direct", "image_tool", "tts_tool"])
async def test_media_generation_does_not_block_text_worker(queue_store, media_mode) -> None:
    """实际绘图占用媒体额度，普通对话提供图片或语音工具也不受影响。"""

    media = text_job(job_id="media", session_id="media").model_dump(by_alias=True)
    media.update(mediaOutput="IMAGE", chatTools=(), backgroundImage=True)
    await queue_store.enqueue(ConversationJob.model_validate(media))
    text = text_job(job_id="text", session_id="text")
    if media_mode != "direct":
        text = text.model_copy(update={"chat_tools": (
            "local_workflow_generate_image" if media_mode == "image_tool" else "tts_generate",
        )})
    await queue_store.enqueue(text)
    first, second = await queue_store.read_new(count=2, block_ms=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    text_started = asyncio.Event()

    async def generate(job):
        """媒体挂起时独立会话的文本仍必须进入生成。"""

        if job.job_id == "media":
            entered.set()
            await release.wait()
        else:
            text_started.set()
        return text_result("done")

    processor = processor_mock()
    processor.generate.side_effect = generate
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(1, 2, 0.01, 0.01, 600), clock=lambda: 1_800_000_000,
    )
    queue._schedule(first)
    await asyncio.wait_for(entered.wait(), 1)
    queue._schedule(second)
    try:
        await asyncio.wait_for(text_started.wait(), 0.2)
    finally:
        release.set()
        await queue.stop()


@pytest.mark.asyncio
async def test_dispatcher_prefetch_stays_bounded_with_one_flooded_session(queue_store) -> None:
    """阻塞会话不能导致读取器将全部 Redis 积压变成等待任务。"""

    queue_store._max_pending = 100
    for index in range(100):
        await queue_store.enqueue(text_job(job_id=f"flood-{index}"))
    processor = processor_mock()
    entered = asyncio.Event()

    async def generate(_job):
        """让已读任务持续占用预取容量。"""

        entered.set()
        await asyncio.Event().wait()

    processor.generate.side_effect = generate
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(1, 2, 0.01, 0.01, 600), clock=lambda: 1_800_000_000,
    )
    await queue.start()
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.sleep(0.03)
        pending = await queue_store._redis.xpending(queue_store._stream_key, "workers")
        assert pending["pending"] <= 8
        assert len(queue._entry_tasks) <= 8
    finally:
        await queue.stop()


@pytest.mark.asyncio
async def test_reclaim_is_bounded_and_does_not_delete_unclaimed_pending(queue_store) -> None:
    """恢复每批只领取容量内条目，旧消费者仍有 pending 时不能删除。"""

    await queue_store.enqueue(text_job(job_id="first"))
    await queue_store.enqueue(text_job(job_id="second"))
    entries = await queue_store.read_new(count=2, block_ms=1)
    replacement = RedisConversationQueueStore(
        queue_store._redis, key_prefix="test:chat", group_name="workers",
        consumer_name="replacement", max_pending=2, result_ttl_seconds=60,
    )
    await replacement.initialize()
    await asyncio.sleep(0.02)
    first = await replacement.reclaim(count=1, min_idle_ms=0)
    assert first == entries[:1]
    consumers = await queue_store._redis.xinfo_consumers(queue_store._stream_key, "workers")
    assert any(item["name"] == "consumer-1" for item in consumers)
    # fakeredis does not update xinfo_consumer counters on xclaim; inspect the actual PEL.
    unclaimed = await queue_store._redis.xpending_range(
        queue_store._stream_key, "workers", min="-", max="+", count=2,
        consumername="consumer-1",
    )
    assert [item["message_id"] for item in unclaimed] == [entries[1].stream_id]
    second = await replacement.reclaim(count=1, min_idle_ms=0)
    assert second == entries[1:]
    assert await replacement.reclaim(count=1, min_idle_ms=0) == []


@pytest.mark.asyncio
async def test_reclaim_keeps_pending_younger_than_idle_threshold(queue_store) -> None:
    """没有达到恢复闲置时限的 pending 不得因清理消费者而丢失。"""

    await queue_store.enqueue(text_job())
    await queue_store.read_new(count=1, block_ms=1)
    replacement = RedisConversationQueueStore(
        queue_store._redis, key_prefix="test:chat", group_name="workers",
        consumer_name="replacement", max_pending=2, result_ttl_seconds=60,
    )
    assert await replacement.reclaim(count=1, min_idle_ms=60_000) == []
    pending = await queue_store._redis.xpending(queue_store._stream_key, "workers")
    assert pending["pending"] == 1


@pytest.mark.asyncio
async def test_session_serialization_covers_ack_and_reset(queue_store, monkeypatch) -> None:
    """Redis 确认未完成时，后续生成与同会话重置都必须继续等待。"""

    for job_id in ("first", "second"):
        await queue_store.enqueue(text_job(job_id=job_id))
    first, second = await queue_store.read_new(count=2, block_ms=1)
    ack_entered = asyncio.Event()
    release_ack = asyncio.Event()
    events: list[str] = []
    complete = queue_store.complete

    async def held_complete(stream_id):
        """暂停首条的最终确认，暴露过早释放会话锁的行为。"""

        if stream_id == first.stream_id:
            ack_entered.set()
            await release_ack.wait()
        await complete(stream_id)
        events.append(f"ack:{stream_id}")

    async def generate(job):
        """记录后续生成有没有越过确认边界。"""

        events.append(f"generate:{job.job_id}")
        return text_result("done")

    monkeypatch.setattr(queue_store, "complete", held_complete)
    processor = processor_mock()
    processor.generate.side_effect = generate
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(2, 2, 0.01, 0.01, 600), clock=lambda: 1_800_000_000,
    )

    async def reset_session():
        """模拟运行时通过队列公开锁执行历史重置。"""

        async with queue.session_guard(first.job.session_id):
            events.append("reset")

    first_task = asyncio.create_task(queue.process_entry(first))
    await asyncio.wait_for(ack_entered.wait(), 1)
    reset_task = asyncio.create_task(reset_session())
    second_task = asyncio.create_task(queue.process_entry(second))
    try:
        await asyncio.sleep(0.02)
        assert events == ["generate:first"]
    finally:
        release_ack.set()
        await asyncio.gather(first_task, reset_task, second_task)
    assert events == [
        "generate:first", f"ack:{first.stream_id}", "reset",
        "generate:second", f"ack:{second.stream_id}",
    ]
    assert queue._session_slots == {}


@pytest.mark.asyncio
async def test_startup_drains_recovery_in_bounded_batches_before_new_jobs(queue_store) -> None:
    """重启逐批恢复全部旧 pending，并保持它们先于新任务处理。"""

    queue_store._max_pending = 21
    for index in range(20):
        await queue_store.enqueue(text_job(job_id=f"old-{index}"))
    await queue_store.read_new(count=20, block_ms=1)
    await queue_store.enqueue(text_job(job_id="new"))
    processor = processor_mock()
    generated: list[str] = []
    complete = asyncio.Event()

    async def generate(job):
        """记录恢复任务与新任务的执行顺序。"""

        generated.append(job.job_id)
        if job.job_id == "new":
            complete.set()
        return text_result("done")

    processor.generate.side_effect = generate
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(1, 2, 0.01, 0.01, 600), clock=lambda: 1_800_000_000,
    )
    await asyncio.sleep(0.02)
    await queue.start()
    try:
        await asyncio.wait_for(complete.wait(), 1)
    finally:
        await queue.stop()
    assert generated == [*(f"old-{index}" for index in range(20)), "new"]


@pytest.mark.asyncio
async def test_ready_session_within_prefetch_buffer_progresses(queue_store) -> None:
    """有限窗口内阻塞会话的后续条目不占执行槽，其他会话仍可执行。"""

    queue_store._max_pending = 4
    for index in range(3):
        await queue_store.enqueue(text_job(job_id=f"busy-{index}"))
    await queue_store.enqueue(text_job(job_id="ready", session_id="ready"))
    ready = asyncio.Event()

    async def generate(job):
        """阻塞首个会话，等待独立会话取得第二个执行槽。"""

        if job.job_id == "ready":
            ready.set()
            return text_result("done")
        await asyncio.Event().wait()

    processor = processor_mock()
    processor.generate.side_effect = generate
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(2, 2, 0.01, 0.01, 600), clock=lambda: 1_800_000_000,
    )
    await queue.start()
    try:
        await asyncio.wait_for(ready.wait(), 0.3)
    finally:
        await queue.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("same_session", [False, True])
async def test_waiting_deadline_expires_before_busy_job_releases(
    queue_store, same_session,
) -> None:
    """会话锁和执行槽的等待本身也受总时限约束，无需等待前项完成。"""

    await queue_store.enqueue(text_job(job_id="busy"))
    waiting = text_job(
        job_id="waiting", session_id="qq_session_1" if same_session else "other",
    ).model_copy(update={"created_at": 1_800_000_000 - 0.96})
    await queue_store.enqueue(waiting)
    first, second = await queue_store.read_new(count=2, block_ms=1)
    busy = asyncio.Event()
    generated: list[str] = []

    async def generate(job):
        """首项保持活跃，等待项必须自己超时结束。"""

        generated.append(job.job_id)
        busy.set()
        await asyncio.Event().wait()

    processor = processor_mock()
    processor.generate.side_effect = generate
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(1, 2, 0.01, 0.01, 1), clock=lambda: 1_800_000_000,
    )
    queue._schedule(first)
    await asyncio.wait_for(busy.wait(), 1)
    try:
        await asyncio.wait_for(queue.process_entry(second), 0.3)
        assert generated == ["busy"]
        assert await queue_store.dead_letter_length() == 1
    finally:
        await queue.stop()
    assert queue._session_slots == {}


@pytest.mark.asyncio
async def test_media_worker_limit_applies_across_independent_sessions(queue_store) -> None:
    """独立会话的媒体请求也不能突破媒体并发上限。"""

    for job_id in ("first", "second"):
        job = text_job(job_id=job_id, session_id=job_id).model_copy(
            update={"chat_tools": (), "media_output": "IMAGE", "background_image": True},
        )
        await queue_store.enqueue(job)
    first, second = await queue_store.read_new(count=2, block_ms=1)
    entered = asyncio.Event()
    release = asyncio.Event()
    generated: list[str] = []

    async def generate(job):
        """第一条媒体持有额度，后续媒体需要等待。"""

        generated.append(job.job_id)
        entered.set()
        await release.wait()
        return text_result("done")

    processor = processor_mock()
    processor.generate.side_effect = generate
    processor.delivery_count.return_value = 1
    queue = ConversationQueue(
        store=queue_store, processor=processor,
        policy=QueuePolicy(4, 2, 0.01, 0.01, 600, media_workers=1),
        clock=lambda: 1_800_000_000,
    )
    first_task = asyncio.create_task(queue.process_entry(first))
    await asyncio.wait_for(entered.wait(), 1)
    second_task = asyncio.create_task(queue.process_entry(second))
    try:
        await asyncio.sleep(0.02)
        assert generated == ["first"]
    finally:
        release.set()
        await asyncio.gather(first_task, second_task)
    assert generated == ["first", "second"]


@pytest.mark.asyncio
async def test_reset_guard_rejects_busy_session_without_waiting(queue_store) -> None:
    """重置持有运行时租约时必须立即拒绝忙碌会话，避免刷新锁顺序反转。"""

    queue = ConversationQueue(
        store=queue_store, processor=processor_mock(),
        policy=QueuePolicy(1, 2, 0.01, 0.01, 600), clock=lambda: 1_800_000_000,
    )

    async def reset():
        """模拟已取得运行时租约的重置处理器。"""

        async with queue.session_guard("busy", wait=False):
            pytest.fail("reset entered an active session")

    async with queue.session_guard("busy"):
        with pytest.raises(ConversationQueueError, match="session"):
            await asyncio.wait_for(reset(), 0.1)
    async with queue.session_guard("busy", wait=False):
        pass
    assert queue._session_slots == {}




@pytest.mark.asyncio
async def test_tool_image_and_existing_media_fit_persisted_budget(queue_store):
    """工具截图与已有媒体各自合规时，组合结果仍可持久化并恢复。"""
    import base64
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * (7 * 1024 * 1024)).decode()
    result = ConversationResult(kind="MEDIA", media=ChatMediaResponse(items=(
        ChatMediaItem(type="IMAGE", mimeType="image/png", data=image),
    )), toolImages=({"mimeType": "image/png", "data": image},))
    await queue_store.save_result("1-0", result)
    restored = await queue_store.load_result("1-0")
    assert restored == result
