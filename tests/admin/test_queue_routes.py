"""死信维护只删除当前队列的失败记录，复用管理认证并保留运行数据。"""

import time
from pathlib import Path
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

from agent_bot.admin.config import AdminConfig
from agent_bot.admin.routes import install_admin
from agent_bot.integrations.redis_queue import RedisConversationQueueStore
from agent_bot.integrations.sqlite_queue import SqliteConversationQueueStore
from agent_bot.services.conversation_queue import ConversationJob, ConversationResult

HEADERS = {"Authorization": "Bearer queue-test-admin"}
URL = "/api/v1/queues/dead-letters"
SCHEMA_PATH = Path(__file__).resolve().parents[2] / "sql/agent_bot_queue_init.sqlite.sql"


@pytest.fixture(params=["sqlite", "redis"])
async def queue_stores(request, tmp_path, monkeypatch):
    """为文本、绘图与其他部署创建隔离存储，完全不连接生产队列。"""
    from agent_bot.integrations import sqlite_queue_database

    monkeypatch.setattr(sqlite_queue_database, "queue_schema_sql", lambda: SCHEMA_PATH.read_text(
        encoding="utf-8",
    ))
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    stores = []
    for namespace in ("test:chat", "test:images", "other:chat"):
        if request.param == "sqlite":
            store = SqliteConversationQueueStore(
                str(tmp_path / "queue.sqlite3"), key_prefix=namespace,
                max_pending=5, result_ttl_seconds=60,
            )
        else:
            store = RedisConversationQueueStore(
                redis, key_prefix=namespace, group_name="test", consumer_name="test",
                max_pending=5, result_ttl_seconds=60,
            )
        await store.initialize()
        stores.append(store)
    try:
        yield tuple(stores)
    finally:
        await redis.aclose()


async def seed_queue(store):
    """每个命名空间保留一项死信和一项已有生成结果的待发送任务。"""
    failed = ConversationJob(
        jobId="failed", requestId="failed", createdAt=time.time(), sessionId="test",
        botId="10001", userId="10002", scene="private", prompt="离线测试",
    )
    await store.enqueue(failed)
    entry = (await store.read_new(count=1, block_ms=1))[0]
    await store.dead_letter(entry, reason="ConnectionClosedError", attempt=2)
    pending = failed.model_copy(update={"job_id": "pending", "request_id": "pending"})
    stream = await store.enqueue(pending)
    result = ConversationResult(
        kind="TEXT", reply={"text": "已经生成", "requestId": "pending"},
        deliveryParts=("已经生成",),
    )
    await store.save_result(stream, result)
    return stream, result


async def test_clear_store_preserves_other_namespaces_and_pending_results(queue_stores):
    """清除只影响当前命名空间，重复清除返回零，不删除待发送结果。"""
    for store in queue_stores:
        await seed_queue(store)
    first, second, other = queue_stores
    assert await first.clear_dead_letters() == 1
    assert await first.clear_dead_letters() == 0
    assert await first.dead_letter_length() == 0
    assert await second.dead_letter_length() == await other.dead_letter_length() == 1
    for store in queue_stores:
        entry = (await store.read_new(count=1, block_ms=1))[0]
        assert entry.job.job_id == "pending"
        assert (await store.load_result(entry.stream_id)).reply.text == "已经生成"
        assert (await store.load_state(entry.stream_id)).result_ready


@pytest.fixture
async def queue_api(admin_store, admin_runtime, queue_stores):
    """真实管理路由使用显式注入的当前队列，不读取启动文件中的数据库。"""
    admin_runtime.queue_stores = queue_stores[:2]
    app = FastAPI()
    install_admin(app, admin_store, admin_runtime, AdminConfig(token="queue-test-admin"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://offline.test",
    ) as client:
        yield client


async def test_clear_dead_letters_requires_auth_and_preserves_active_tasks(queue_api, queue_stores):
    """管理员一次清理文本和绘图死信，未认证请求不得产生副作用。"""
    for store in queue_stores:
        await seed_queue(store)
    assert (await queue_api.delete(URL)).status_code == 401
    assert (await queue_api.get(URL)).status_code == 401
    counts = await queue_api.get(URL, headers=HEADERS)
    assert counts.json() == {"available": True, "text": 1, "images": 1, "total": 2}
    response = await queue_api.delete(URL, headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"cleared": 2, "text": 1, "images": 1}
    assert (await queue_api.delete(URL, headers=HEADERS)).json()["cleared"] == 0
    assert (await queue_api.get(URL, headers=HEADERS)).json()["total"] == 0
    assert await queue_stores[2].dead_letter_length() == 1
    for store in queue_stores:
        assert await store.source_length() == 1


async def test_queue_maintenance_unavailable_when_plugin_is_not_running(queue_api, admin_runtime):
    """禁用或尚未启动的队列必须显示不可用，不能自动连接其他后端。"""
    admin_runtime.queue_stores = ()
    response = await queue_api.get(URL, headers=HEADERS)
    assert response.json() == {"available": False, "text": 0, "images": 0, "total": 0}
    response = await queue_api.delete(URL, headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "QUEUE_UNAVAILABLE"


async def test_runtime_providers_only_expose_running_owned_queues(admin_runtime):
    """宿主和兼容入口只暴露已有队列，停用插件时不得导入或构造新实例。"""
    from tests.unit.test_plugins import FakePlugin

    from agent_bot.admin.application_runtime import ApplicationRuntimeProvider
    from agent_bot.admin.runtime import PluginRuntimeProvider
    from agent_bot.plugins.contracts import BuiltinPlugin, PluginMetadata
    from agent_bot.plugins.manager import PluginManager
    from agent_bot.plugins.qqbot.plugin import QQBotPlugin

    stores = (object(), object())
    plugin = QQBotPlugin()
    plugin._module = SimpleNamespace(
        conversation_queue=SimpleNamespace(store=stores[0]),
        image_queue=SimpleNamespace(store=stores[1]),
    )
    assert plugin.queue_stores() == stores
    assert PluginRuntimeProvider(plugin._module).queue_stores == stores
    metadata = PluginMetadata("qqbot", "QQ", "test", "离线测试")
    fake = FakePlugin(metadata, [])
    fake.queue_stores = lambda: stores
    manager = PluginManager([BuiltinPlugin(metadata, lambda: fake)], ["qqbot"])
    manager.prepare(SimpleNamespace())
    provider = ApplicationRuntimeProvider(admin_runtime, manager)
    assert provider.queue_stores == ()
    await manager.start()
    assert provider.queue_stores == stores
    await manager.stop()
    assert provider.queue_stores == ()
