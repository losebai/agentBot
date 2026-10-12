"""绘图任务归属、持久化取消与投递边界回归。"""
import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from agent_bot.integrations.redis_queue import RedisConversationQueueStore
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationQueue,
    ConversationResult,
    QueuePolicy,
)
from agent_bot.services.image_task_context import active_image_task


def image_job(task_id="image-1"):
    """构造稳定的后台绘图任务。"""
    return ConversationJob(jobId=task_id, requestId=task_id, createdAt=time.time(),
        sessionId="group-1", botId="90001", scene="group", groupId="20001",
        userId="10001", prompt="draw", backgroundImage=True, mediaOutput="IMAGE")


def owner(**changes):
    """仅将可信机器人、会话、用户组合作为任务归属。"""
    from agent_bot.services.image_tasks import ImageTaskOwner
    return ImageTaskOwner(**(dict(bot_id="90001", session_id="group-1", user_id="10001") | changes))


@pytest.fixture
async def image_queue():
    """使用真实 Redis 队列与本地存储替身，隔离外部发送。"""
    from agent_bot.integrations.redis_image_tasks import RedisImageTaskStore
    from agent_bot.services.image_tasks import ImageTaskManager
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    tasks = RedisImageTaskStore(redis, key_prefix="test:images", result_ttl_seconds=60,
                               job_ttl_seconds=600)
    store = RedisConversationQueueStore(redis, key_prefix="test:images", group_name="workers",
        consumer_name="worker", max_pending=30, result_ttl_seconds=60, image_task_store=tasks)
    await store.initialize()
    manager = ImageTaskManager(tasks, store)
    processor = AsyncMock()
    processor.retryable_errors = (ConnectionError,)
    processor.generate.return_value = ConversationResult(kind="SILENT")
    processor.delivery_count.return_value = 0
    queue = ConversationQueue(store=store, processor=processor, image_tasks=manager,
        policy=QueuePolicy(workers=1, max_attempts=2, retry_initial_seconds=.01,
                           retry_max_seconds=.01, job_ttl_seconds=600))
    yield SimpleNamespace(queue=queue, manager=manager, processor=processor, redis=redis)
    await redis.aclose()


async def test_image_queue_exposes_optional_manager():
    """普通队列必须保持兼容，图片管理为显式可选能力。"""
    queue = ConversationQueue(store=AsyncMock(), processor=AsyncMock(),
        policy=QueuePolicy(workers=1, max_attempts=1, retry_initial_seconds=1,
                           retry_max_seconds=1, job_ttl_seconds=60))
    assert queue.image_tasks is None


async def test_list_ownership_limit_and_cancel_authorization(image_queue):
    """跨机器人、会话、用户都不能列举或取消别人的任务。"""
    from agent_bot.services.image_tasks import ImageTaskError
    env = image_queue
    for index in range(23):
        await env.queue.enqueue(image_job(f"image-{index}"))
    assert len((await env.manager.list_tasks(owner(), limit=99)).tasks) == 20
    assert (await env.manager.list_tasks(owner(), limit=1)).tasks[0].task_id == "image-22"
    for other in (owner(bot_id="90002"), owner(session_id="group-2"), owner(user_id="10002")):
        assert (await env.manager.list_tasks(other)).tasks == []
        with pytest.raises(ImageTaskError) as exc:
            await env.manager.cancel(other, "image-22")
        assert exc.value.code == "image_task_not_found"


async def test_cancel_preloaded_frees_capacity(image_queue):
    """取消已预取任务后即使从旧内存条目恢复，也不执行和发送。"""
    env = image_queue
    await env.queue.enqueue(image_job())
    entry = (await env.queue.store.read_new(count=1, block_ms=1))[0]
    result = await env.manager.cancel(owner(), "image-1")
    assert result.task.status == "cancelled"
    assert await env.queue.store.source_length() == 0
    await env.queue.process_entry(entry)
    assert env.processor.generate.await_count == 0
    assert env.processor.deliver_failure.await_count == 0


async def test_cancel_during_submission(image_queue, monkeypatch):
    """POST 期间取消不会丢失返回的 prompt，必须向原服务执行定点取消。"""
    env = image_queue
    started, release = asyncio.Event(), asyncio.Event()
    seen = []

    class Client:
        """验证定点取消实际使用持久化地址与 prompt。"""
        def __init__(self, *, base_url, timeout_seconds):
            """记录原始服务。"""
            self.base_url = base_url
        async def cancel(self, prompt_id):
            """记录特定上游任务。"""
            seen.append((self.base_url, prompt_id))
            return SimpleNamespace(status="cancelled")
        async def aclose(self):
            """无需清理外部连接。"""

    monkeypatch.setattr("agent_bot.integrations.comfy.ComfyClient", Client)

    async def generate(job):
        """模拟请求在取消后才返回上游任务号。"""
        context = active_image_task.get()
        await context.submitting("http://original:8188/")
        started.set()
        await release.wait()
        await context.submitted("prompt-1", "http://original:8188/")
        return ConversationResult(kind="SILENT")

    env.processor.generate.side_effect = generate
    await env.queue.enqueue(image_job())
    entry = (await env.queue.store.read_new(count=1, block_ms=1))[0]
    execution = asyncio.create_task(env.queue.process_entry(entry))
    await asyncio.wait_for(started.wait(), 2)
    result = await env.manager.cancel(owner(), "image-1")
    assert result.task.upstream_cancellation == "pending"
    release.set()
    await asyncio.wait_for(execution, 2)
    assert seen == [("http://original:8188/", "prompt-1")]
    assert (await env.manager.list_tasks(owner())).tasks[0].upstream_cancellation == "cancelled"
    assert env.processor.deliver_failure.await_count == 0


async def test_cancel_failure_suppresses_retry(image_queue):
    """主动取消后普通网络错误不能导致重试或错误回复。"""
    env = image_queue
    async def generate(job):
        """模拟生成期间用户取消后出现网络故障。"""
        await env.manager.cancel(owner(), job.job_id)
        raise ConnectionError("offline")
    env.processor.generate.side_effect = generate
    await env.queue.enqueue(image_job())
    entry = (await env.queue.store.read_new(count=1, block_ms=1))[0]
    await env.queue.process_entry(entry)
    assert env.processor.generate.await_count == 1
    assert env.processor.deliver_failure.await_count == 0
    assert await env.queue.store.dead_letter_length() == 0


async def test_completion_and_shutdown(image_queue):
    """正常完成持久化终态；关闭时保留任务且不得误记主动取消。"""
    env = image_queue
    await env.queue.enqueue(image_job())
    entry = (await env.queue.store.read_new(count=1, block_ms=1))[0]
    await env.queue.process_entry(entry)
    assert (await env.manager.list_tasks(owner())).tasks[0].status == "completed"
    assert (await env.manager.cancel(owner(), "image-1")).task.status == "completed"
    await env.queue.enqueue(image_job("image-2"))
    entry = (await env.queue.store.read_new(count=1, block_ms=1))[0]
    env.processor.generate.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await env.queue.process_entry(entry)
    assert (await env.manager.list_tasks(owner())).tasks[0].cancellation_requested is False
    assert await env.queue.store.source_length() == 1


async def test_cancel_waits_delivery_and_blocks_next_part(image_queue):
    """取消返回之前等待已开始发送的片段，此后禁止下一片段。"""
    env = image_queue
    entered, release = asyncio.Event(), asyncio.Event()
    sent = []
    env.processor.generate.return_value = ConversationResult(
        kind="TEXT", reply=dict(text="ab", requestId="r"), deliveryParts=("a", "b"))
    env.processor.delivery_count.return_value = 2
    async def deliver(job, result, index):
        """阻塞第一片段暴露竞争。"""
        entered.set()
        await release.wait()
        sent.append(index)
    env.processor.deliver_part.side_effect = deliver
    await env.queue.enqueue(image_job())
    entry = (await env.queue.store.read_new(count=1, block_ms=1))[0]
    execution = asyncio.create_task(env.queue.process_entry(entry))
    await asyncio.wait_for(entered.wait(), 2)
    cancellation = asyncio.create_task(env.manager.cancel(owner(), "image-1"))
    await asyncio.sleep(0)
    assert not cancellation.done()
    release.set()
    await asyncio.wait_for(cancellation, 2)
    await asyncio.wait_for(execution, 2)
    assert sent == [0]
    assert env.processor.deliver_failure.await_count == 0


async def test_cancel_intent_survives_stale_save_and_restart(image_queue):
    """取消标志不会被之前读出的状态覆盖，重建管理器后仍然有效。"""
    from agent_bot.services.image_tasks import ImageTaskManager
    env = image_queue
    await env.queue.enqueue(image_job())
    stale = await env.manager.store.get("image-1")
    await env.manager.cancel(owner(), "image-1")
    stale.status = "running"
    await env.manager.store.save(stale)
    restarted = ImageTaskManager(env.manager.store, env.queue.store)
    result = (await restarted.list_tasks(owner())).tasks[0]
    assert result.cancellation_requested is True
    assert result.status == "cancelled"


async def test_failed_known_prompt_can_still_cancel_upstream(image_queue, monkeypatch):
    """本地超时失败不代表 GPU 停止，保留按上游 ID 取消能力。"""
    env = image_queue
    await env.queue.enqueue(image_job())
    record = await env.manager.store.get("image-1")
    record.status = "failed"
    record.started = True
    record.settled = True
    record.endpoint = "http://original:8188"
    record.prompt_id = "still-running"
    await env.manager.store.save(record)
    client = AsyncMock()
    client.cancel.return_value = SimpleNamespace(status="unsupported")
    monkeypatch.setattr("agent_bot.integrations.comfy.ComfyClient", lambda **kwargs: client)
    result = await env.manager.cancel(owner(), "image-1")
    assert result.task.cancellation_requested is True
    assert result.task.upstream_cancellation == "unsupported"
    client.cancel.assert_awaited_once_with("still-running")


async def test_cancel_live_record_never_expires(image_queue):
    """运行期间取消不可设置 TTL，避免长 HTTP 请求完成后取消标志消失。"""
    env = image_queue
    await env.queue.enqueue(image_job())
    execution = env.manager.execution("image-1")
    await execution.transition("running")
    result = await env.manager.cancel(owner(), "image-1")
    assert result.task.upstream_cancellation == "unsupported"
    key = env.manager.store._key("image-1")
    assert await env.redis.ttl(key) == -1
    await execution.finish("cancelled")
    assert await env.redis.ttl(key) >= 600


async def test_legacy_preloaded_result_is_adopted_without_regeneration(image_queue):
    """升级前已入队并保存结果的任务必须照常恢复投递。"""
    env = image_queue
    env.queue.store._image_task_store = None
    stream_id = await env.queue.enqueue(image_job())
    entry = (await env.queue.store.read_new(count=1, block_ms=1))[0]
    await env.queue.store.save_result(stream_id, ConversationResult(kind="SILENT"))
    await env.queue.process_entry(entry)
    assert env.processor.generate.await_count == 0
    assert env.processor.commit.await_count == 1
    assert (await env.manager.list_tasks(owner())).tasks[0].status == "completed"


async def test_transient_metadata_lookup_recovers_without_restart(image_queue, monkeypatch):
    """初始元数据查询故障应在当前工作任务恢复，不能遗留 pending。"""
    from redis.exceptions import RedisError
    env = image_queue
    await env.queue.enqueue(image_job())
    entry = (await env.queue.store.read_new(count=1, block_ms=1))[0]
    original = env.manager.store.get
    calls = 0
    async def get(task_id):
        """仅第一次读取模拟断连。"""
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RedisError("offline")
        return await original(task_id)
    monkeypatch.setattr(env.manager.store, "get", get)
    await env.queue.process_entry(entry)
    assert await env.queue.store.source_length() == 0
    assert env.processor.commit.await_count == 1


@pytest.mark.parametrize("max_attempts", [1, 2])
async def test_terminal_ack_failure_never_regenerates_or_redelivers(
    image_queue, monkeypatch, max_attempts,
):
    """确认 Stream 时临时故障，恢复必须只做清理。"""
    from redis.exceptions import RedisError
    env = image_queue
    env.queue.policy = replace(env.queue.policy, max_attempts=max_attempts)
    await env.queue.enqueue(image_job())
    entry = (await env.queue.store.read_new(count=1, block_ms=1))[0]
    original = env.queue.store.complete
    calls = 0
    async def complete(stream_id):
        """模拟 Redis 已完成删除但客户端未收到确认。"""
        nonlocal calls
        calls += 1
        await original(stream_id)
        if calls == 1:
            raise RedisError("ambiguous ACK")
    monkeypatch.setattr(env.queue.store, "complete", complete)
    await env.queue.process_entry(entry)
    assert env.processor.generate.await_count == 1
    assert env.processor.commit.await_count == 1
    assert (await env.manager.list_tasks(owner())).tasks[0].status == "completed"
