"""真实 SQLite 下的图片取消和工具副作用跨实例一致性。"""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from agent_bot.services.image_tasks import ImageTaskError, ImageTaskOwner, ImageTaskRecord
from agent_bot.services.tool_operations import MentionEffect, ReplyEffect, ToolOperationConflict


@pytest.fixture
def database(tmp_path: Path) -> str:
    """使用队列的正式初始化脚本创建独立数据库。"""
    path = tmp_path / "queue.sqlite3"
    script = Path(__file__).parents[2] / "sql" / "agent_bot_queue_init.sqlite.sql"
    with sqlite3.connect(path) as connection:
        connection.executescript(script.read_text(encoding="utf-8"))
    return str(path)


def image_store(path: str, prefix: str = "test"):
    """构造共享文件但无共享内存的任务存储。"""
    from agent_bot.integrations.sqlite_image_tasks import SqliteImageTaskStore

    return SqliteImageTaskStore(
        path, key_prefix=prefix, result_ttl_seconds=60, job_ttl_seconds=600,
    )


def tool_store(path: str, prefix: str = "test"):
    """构造工具记录存储，便于模拟进程重启。"""
    from agent_bot.integrations.sqlite_tool_operations import SqliteToolOperationStore

    return SqliteToolOperationStore(path, key_prefix=prefix, ttl_seconds=60)


async def test_video_receipt_reopens_without_video_bytes(database):
    """SQLite 默认队列跨实例恢复视频引用，不能把视频内容序列化进库。"""
    from agent_bot.services.tool_operations import VideoEffect

    original = tool_store(database)
    effect = VideoEffect(workflow_key="h3", asset_id="a" * 32)
    assert await original.begin("video-job", "local_workflow_generate_video", "b" * 64) is None
    await original.complete("video-job", "local_workflow_generate_video", "b" * 64, effect)
    restored = await tool_store(database).begin(
        "video-job", "local_workflow_generate_video", "b" * 64
    )
    assert restored == effect
    assert "data" not in restored.model_dump()


def task(task_id: str = "image-1", created_at: float = 1000) -> ImageTaskRecord:
    """构造具有可信归属的活动任务。"""
    return ImageTaskRecord(
        task_id=task_id, created_at=created_at, stream_id="1",
        owner=ImageTaskOwner(bot_id="90001", session_id="group-1", user_id="10001"),
    )


async def test_image_cancel_survives_stale_save_and_adopt(database: str) -> None:
    """旧工作协程和恢复者不能清除已确认取消的意图。"""
    store = image_store(database)
    stale = task()
    await store.adopt(stale)
    await store.request_cancel(stale.owner, stale.task_id)
    stale.status = "running"
    stale.submitting = True
    await image_store(database).save(stale)
    assert stale.cancellation_requested
    await store.adopt(task())
    saved = await image_store(database).get(stale.task_id)
    assert saved.status == "cancelled"
    assert saved.upstream_cancellation == "pending"


async def test_image_owner_and_namespace_boundaries(database: str) -> None:
    """查询与取消必须同时匹配机器人、会话、用户及存储命名空间。"""
    store = image_store(database)
    record = task()
    await store.adopt(record)
    for field in ("bot_id", "session_id", "user_id"):
        other = record.owner.model_copy(update={field: "other"})
        assert await store.recent(other, 20) == []
        with pytest.raises(ImageTaskError):
            await store.request_cancel(other, record.task_id)
    assert await image_store(database, "other").get(record.task_id) is None
    for index in range(25):
        await store.adopt(task(f"task-{index}", 1001 + index))
    records = await store.recent(record.owner, 100)
    assert len(records) == 20
    assert records[0].task_id == "task-24"


async def test_image_active_lifetime_and_cancel_budget(database: str, monkeypatch) -> None:
    """活动任务不过期，取消终态至少覆盖旧协程的剩余任务寿命。"""
    monkeypatch.setattr("agent_bot.integrations.sqlite_image_tasks.time.time", lambda: 1000)
    store = image_store(database)
    await store.adopt(task())
    monkeypatch.setattr("agent_bot.integrations.sqlite_image_tasks.time.time", lambda: 10000)
    assert await store.get("image-1") is not None
    monkeypatch.setattr("agent_bot.integrations.sqlite_image_tasks.time.time", lambda: 1000)
    record = await store.request_cancel(task().owner, "image-1")
    record.settled = True
    await store.save(record)
    monkeypatch.setattr("agent_bot.integrations.sqlite_image_tasks.time.time", lambda: 1601)
    assert await store.get("image-1") is not None
    monkeypatch.setattr("agent_bot.integrations.sqlite_image_tasks.time.time", lambda: 1661)
    assert await store.get("image-1") is None
    assert await store.recent(record.owner, 20) == []


async def test_image_enqueue_joins_outer_transaction(database: str) -> None:
    """队列事务回滚时不能留下不曾成功入队的图片任务。"""
    from agent_bot.services.conversation_queue import ConversationJob

    store = image_store(database)
    job = ConversationJob(
        jobId="atomic", requestId="atomic", createdAt=1000, sessionId="group-1",
        botId="90001", userId="10001", scene="group", groupId="20001", prompt="draw",
    )
    connection = sqlite3.connect(database)
    try:
        connection.execute("BEGIN IMMEDIATE")
        store.enqueue(connection, job, "42")
        connection.rollback()
        assert await store.get("atomic") is None
        with connection:
            store.enqueue(connection, job, "43")
        assert (await store.get("atomic")).stream_id == "43"
    finally:
        connection.close()


async def test_tool_concurrent_claim_and_restart(database: str) -> None:
    """并发或重启不能重复取得副作用执行权，完成后可恢复原结果。"""
    results = await asyncio.gather(
        *(tool_store(database).begin("job", "mention_member", "a" * 64) for _ in range(8)),
        return_exceptions=True,
    )
    assert results.count(None) == 1
    assert sum(isinstance(result, ToolOperationConflict) for result in results) == 7
    store = tool_store(database)
    with pytest.raises(ToolOperationConflict):
        await store.begin("job", "mention_member", "a" * 64)
    effect = MentionEffect(message_id=9, target_user_id=10002)
    await store.complete("job", "mention_member", "a" * 64, effect)
    assert await tool_store(database).begin("job", "mention_member", "a" * 64) == effect
    assert await tool_store(database).completed("job", "mention_member") == effect
    assert await tool_store(database, "other").completed("job", "mention_member") is None
    with pytest.raises(ToolOperationConflict):
        await store.begin("job", "mention_member", "b" * 64)


async def test_tool_completion_validates_identity_type_and_expiry(
    database: str, monkeypatch,
) -> None:
    """错误工具类型、参数和过期执行权都不能固化副作用结果。"""
    monkeypatch.setattr("agent_bot.integrations.sqlite_tool_operations.time.time", lambda: 1000)
    store = tool_store(database)
    with pytest.raises(ValueError):
        await store.begin("", "send_reply", "a" * 64)
    await store.begin("job", "mention_member", "a" * 64)
    with pytest.raises(ValueError):
        await store.complete("job", "mention_member", "a" * 64, ReplyEffect(text="hello"))
    result = MentionEffect(target_user_id=10002)
    with pytest.raises(ToolOperationConflict):
        await store.complete("job", "mention_member", "b" * 64, result)
    monkeypatch.setattr("agent_bot.integrations.sqlite_tool_operations.time.time", lambda: 1061)
    with pytest.raises(ToolOperationConflict):
        await store.complete("job", "mention_member", "a" * 64, result)
    assert await store.completed("job", "mention_member") is None
