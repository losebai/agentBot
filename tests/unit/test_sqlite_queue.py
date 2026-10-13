"""本地持久队列的崩溃恢复、原子去重及取消边界测试。"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from agent_bot.config import RuntimeConfig
from agent_bot.services.conversation_queue import (
    ConversationEntryDiscardedError,
    ConversationJob,
    ConversationQueue,
    ConversationQueueFullError,
    ConversationResult,
    QueuePolicy,
)
from agent_bot.services.image_tasks import ImageTaskOwner


@pytest.fixture(autouse=True)
def queue_schema(monkeypatch):
    """源码测试直接读取新增结构，安装包通过打包资源提供同一脚本。"""
    try:
        module = importlib.import_module("agent_bot.integrations.sqlite_queue_database")
    except ModuleNotFoundError:
        return
    schema = Path(__file__).resolve().parents[2] / "sql" / "agent_bot_queue_init.sqlite.sql"
    monkeypatch.setattr(module, "queue_schema_sql", lambda: schema.read_text(encoding="utf-8"))


def local_store(path: Path, **kwargs):
    """延迟导入，以明确验证缺失的本地队列能力。"""
    module = importlib.import_module("agent_bot.integrations.sqlite_queue")
    return module.SqliteConversationQueueStore(
        str(path), key_prefix=kwargs.pop("key_prefix", "test:chat"),
        max_pending=kwargs.pop("max_pending", 2), result_ttl_seconds=60, **kwargs,
    )


def job(identifier: str = "job-1", *, image: bool = False) -> ConversationJob:
    """构造无网络依赖且具有真实有效目标的任务。"""
    return ConversationJob(
        job_id=identifier, request_id=identifier, created_at=time.time(),
        session_id="private-10001", bot_id="90001", scene="private", user_id="10001",
        prompt="你好", background_image=image, media_output="IMAGE" if image else None,
    )


def result() -> ConversationResult:
    """创建两段结果，验证重启后只补发尚未成功的片段。"""
    return ConversationResult(
        kind="TEXT", reply={"text": "第一段第二段", "requestId": "request"},
        delivery_parts=("第一段", "第二段"),
    )


def runtime_config() -> RuntimeConfig:
    """只提供测试启动必需的固定凭据，不读取开发环境文件。"""
    return RuntimeConfig(
        onebot_v11_access_token="test-onebot-access-token-that-is-at-least-32-characters",
        chat_session_secret="test-session-secret-that-is-at-least-32-characters",
    )


async def test_reopen_restores_pending_in_order_and_all_checkpoints(tmp_path):
    """文件重开保留生成结果及两种投递进度，并先恢复旧任务。"""
    path = tmp_path / "nested" / "queue.sqlite3"
    first = local_store(path)
    await first.initialize()
    identifier = await first.enqueue(job())
    await first.read_new(count=1, block_ms=1)
    await first.save_image_start_delivery_index(identifier, 2)
    await first.save_image_start_delivered(identifier)
    await first.save_result(identifier, result())
    await first.save_delivery_index(identifier, 1)
    await first.save_retry(identifier, attempt=2, available_at=time.time() + 10)
    new_identifier = await first.enqueue(job("job-2"))

    restored = local_store(path)
    await restored.initialize()
    recovered = await restored.reclaim(count=1, min_idle_ms=0)
    assert [entry.stream_id for entry in recovered] == [identifier]
    state = await restored.load_state(identifier)
    assert state.attempt == 2 and state.available_at > time.time()
    assert state.delivery_index == 1 and state.result_ready
    assert state.image_start_delivery_index == 2 and state.image_start_delivered
    assert await restored.load_result(identifier) == result()
    assert await restored.reclaim(count=1, min_idle_ms=0) == []
    assert restored.recovery_complete
    assert [entry.stream_id for entry in await restored.read_new(count=1, block_ms=1)] == [
        new_identifier,
    ]


async def test_concurrent_duplicate_is_atomic_and_survives_completion(tmp_path):
    """重复后台任务只占一个容量，完成后回执仍阻止再次生成。"""
    store = local_store(tmp_path / "queue.sqlite3", max_pending=1)
    await store.initialize()
    task = job(image=True)
    identifiers = await asyncio.gather(*(store.enqueue(task) for _ in range(6)))
    assert len(set(identifiers)) == 1
    assert await store.source_length() == 1
    with pytest.raises(ValueError, match="parameters changed"):
        await store.enqueue(task.model_copy(update={"prompt": "改变参数"}))
    with pytest.raises(ConversationQueueFullError):
        await store.enqueue(job("job-2"))
    await store.complete(identifiers[0])
    assert await store.enqueue(task) == identifiers[0]
    assert await store.source_length() == 0


async def test_namespaces_isolate_capacity_and_deletion(tmp_path):
    """同一本地文件中的文本和图片队列互不占用配额或误删状态。"""
    path = tmp_path / "queue.sqlite3"
    first, second = local_store(path, max_pending=1), local_store(path, key_prefix="image")
    await first.initialize()
    await second.initialize()
    identifier = await first.enqueue(job())
    await second.enqueue(job())
    await second.complete(identifier)
    assert await first.source_length() == 1
    assert await second.source_length() == 1


async def test_deadletters_are_bounded_and_corrupt_payload_is_not_replayed(tmp_path):
    """损坏载荷隔离后继续处理正常记录，死信数量有界。"""
    path = tmp_path / "queue.sqlite3"
    store = local_store(path, max_pending=1)
    await store.initialize()
    for index in range(3):
        identifier = await store.enqueue(job(f"job-{index}"))
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE chat_queue_jobs SET payload = ? WHERE stream_id = ?", ("{}", identifier),
            )
        assert await store.read_new(count=1, block_ms=1) == []
    assert await store.source_length() == 0
    assert await store.dead_letter_length() == 1
    identifier = await store.enqueue(job("valid"))
    assert (await store.read_new(count=1, block_ms=1))[0].stream_id == identifier


async def test_corrupt_result_is_discarded_and_expiry_preserves_ready_flag(tmp_path):
    """坏结果不得重生成或发送，过期结果仍保留防止重生成的检查点。"""
    path = tmp_path / "queue.sqlite3"
    store = local_store(path)
    await store.initialize()
    identifier = await store.enqueue(job())
    await store.save_result(identifier, result())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE chat_queue_jobs SET result_expires_at = 0 WHERE stream_id = ?", (identifier,),
        )
    assert await store.load_result(identifier) is None
    assert (await store.load_state(identifier)).result_ready
    await store.save_result(identifier, result())
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE chat_queue_jobs SET result = '{}' WHERE stream_id = ?", (identifier,),
        )
    with pytest.raises(ConversationEntryDiscardedError):
        await store.load_result(identifier)
    assert await store.source_length() == 0
    assert await store.dead_letter_length() == 1


async def test_recovered_delivery_uses_saved_result_without_generation(tmp_path):
    """真正运行队列编排，重启恢复只投递第二段且最终释放容量。"""
    path = tmp_path / "queue.sqlite3"
    store = local_store(path)
    await store.initialize()
    identifier = await store.enqueue(job())
    await store.read_new(count=1, block_ms=1)
    await store.save_result(identifier, result())
    await store.save_delivery_index(identifier, 1)
    restored = local_store(path)
    await restored.initialize()
    entry = (await restored.reclaim(count=1, min_idle_ms=0))[0]
    processor = AsyncMock()
    processor.retryable_errors = ()
    processor.delivery_count.return_value = 2
    queue = ConversationQueue(
        store=restored, processor=processor,
        policy=QueuePolicy(workers=1, max_attempts=2, retry_initial_seconds=0.01,
                           retry_max_seconds=0.01, job_ttl_seconds=600),
    )
    await queue.process_entry(entry)
    processor.generate.assert_not_awaited()
    processor.deliver_part.assert_awaited_once_with(entry.job, result(), 1)
    processor.commit.assert_awaited_once()
    assert await restored.source_length() == 0


async def test_image_enqueue_rolls_back_job_receipt_and_metadata_together(tmp_path, monkeypatch):
    """图片元数据写入失败必须回滚队列和回执，后续重试可真正重新入队。"""
    from agent_bot.integrations.sqlite_image_tasks import SqliteImageTaskStore

    path = tmp_path / "queue.sqlite3"
    images = SqliteImageTaskStore(
        str(path), key_prefix="test:chat", result_ttl_seconds=60, job_ttl_seconds=600,
    )
    store = local_store(path, image_task_store=images)
    await store.initialize()
    original = images.enqueue

    def fail_after_metadata(connection, task, identifier):
        """在事务的最后阶段故障，验证前面的写入也未泄漏。"""
        original(connection, task, identifier)
        raise RuntimeError("metadata write failed")

    monkeypatch.setattr(images, "enqueue", fail_after_metadata)
    task = job(image=True)
    with pytest.raises(RuntimeError, match="metadata write failed"):
        await store.enqueue(task)
    assert await store.source_length() == 0
    assert await images.get(task.job_id) is None
    monkeypatch.setattr(images, "enqueue", original)
    identifier = await store.enqueue(task)
    assert (await images.get(task.job_id)).stream_id == identifier


@pytest.mark.parametrize("started", [False, True])
async def test_image_cancellation_releases_capacity_and_never_delivers(tmp_path, started):
    """排队和生成期间取消均阻止之后投递，取消终态持续可查询。"""
    from agent_bot.integrations.sqlite_queue import build_sqlite_conversation_queue

    generating, release = asyncio.Event(), asyncio.Event()

    async def generate(_job):
        """使用可控事件模拟耗时媒体生成，不访问外部服务。"""
        generating.set()
        await release.wait()
        return result()

    processor = AsyncMock()
    processor.retryable_errors = ()
    processor.generate.side_effect = generate
    resources = build_sqlite_conversation_queue(
        str(tmp_path / "queue.sqlite3"), "test", runtime_config(), processor,
    )
    store, manager = resources.image_queue.store, resources.image_queue.image_tasks
    await store.initialize()
    task = job(image=True)
    await store.enqueue(task)
    entry = (await store.read_new(count=1, block_ms=1))[0]
    worker = None
    if started:
        worker = asyncio.create_task(resources.image_queue.process_entry(entry))
        await asyncio.wait_for(generating.wait(), timeout=3)
    owner = ImageTaskOwner(bot_id=task.bot_id, session_id=task.session_id, user_id=task.user_id)
    cancelled = await manager.cancel(owner, task.job_id)
    assert cancelled.task.status == "cancelled"
    release.set()
    if worker is not None:
        await asyncio.wait_for(worker, timeout=3)
    else:
        await resources.image_queue.process_entry(entry)
    assert await store.source_length() == 0
    record = await manager.store.get(task.job_id)
    assert record.cancellation_requested and record.settled
    processor.deliver_part.assert_not_awaited()
    processor.commit.assert_not_awaited()
    await resources.aclose()


async def test_transaction_cancellation_waits_for_commit_before_returning(tmp_path):
    """线程无法中止时，协程取消必须等待事务完成，避免关闭后隐藏写入。"""
    from agent_bot.integrations.sqlite_queue_database import run_sqlite

    path = tmp_path / "queue.sqlite3"
    store = local_store(path)
    await store.initialize()
    entered, release = threading.Event(), threading.Event()

    def blocked_write(connection):
        """在真实事务中暂停提交，以检查取消与连接释放的先后顺序。"""
        connection.execute(
            "INSERT INTO chat_queue_jobs(namespace, payload, state) VALUES (?, ?, ?)",
            ("test:chat", job().model_dump_json(), "{}"),
        )
        entered.set()
        if not release.wait(timeout=3):
            raise TimeoutError("test did not release transaction")

    pending = asyncio.create_task(run_sqlite(str(path), blocked_write))
    assert await asyncio.to_thread(entered.wait, 3)
    pending.cancel()
    await asyncio.sleep(0.01)
    assert not pending.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert await store.source_length() == 1


async def test_missing_database_and_bad_schema_are_permanent_errors(tmp_path):
    """运行期访问不能偷偷创建数据库，也不能将结构错误无限重试。"""
    path = tmp_path / "queue.sqlite3"
    store = local_store(path)
    with pytest.raises(sqlite3.OperationalError):
        await store.source_length()
    assert not path.exists()
    await store.initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("DROP TABLE chat_queue_jobs")
    with pytest.raises(sqlite3.OperationalError) as error:
        await store.source_length()
    assert not isinstance(error.value, store.transient_errors)


async def test_builder_starts_and_stops_both_dispatchers_without_redis(tmp_path):
    """完整装配可启动两个本地工作队列，并可靠释放各自调度协程。"""
    from agent_bot.integrations.sqlite_queue import build_sqlite_conversation_queue

    processor = AsyncMock()
    processor.retryable_errors = ()
    resources = build_sqlite_conversation_queue(
        str(tmp_path / "queue.sqlite3"), "test", runtime_config(), processor,
    )
    try:
        await resources.queue.start()
        await resources.image_queue.start()
        assert await resources.ping()
        assert resources.queue.running and resources.image_queue.running
    finally:
        await resources.aclose()
    assert not resources.queue.running and not resources.image_queue.running
