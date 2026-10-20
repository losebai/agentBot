"""管理员新增与 QQ 召回共享 HMAC 身份，新增和删除不覆写原文。"""

import asyncio
import sqlite3

import pytest

from agent_bot.database import initialize_database
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.knowledge.contracts import MemoryCreate
from agent_bot.knowledge.errors import KnowledgeError
from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity
from agent_bot.settings import DatabaseParams


@pytest.fixture
def managed_memory(tmp_path):
    """隔离业务库使用生产 SQLite 连接，不模拟授权查询。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "memory.db"))
    initialize_database(params)
    return TaggedMemoryStore(params, secret="offline-memory-secret"), params


@pytest.mark.parametrize(
    "scope,target",
    [("global", None), ("user", "10002"), ("group", "10003"), ("session", "qq_" + "a" * 32)],
)
def test_admin_added_memory_is_visible_only_to_matching_identity(managed_memory, scope, target):
    """手工新增不另建命名空间，也不跨机器人共享私人事实。"""
    store, _ = managed_memory
    created = store.admin_create(
        MemoryCreate(
            scope=scope,
            bot_id="10001",
            target=target,
            key="语言偏好",
            content="使用中文",
            tags=["语言"],
        )
    )
    identity = MemoryIdentity(
        bot_id="10001", user_id="10002", group_id="10003", session_id="qq_" + "a" * 32
    )
    read = MemoryArguments(action="recall", scope="all", tags=["语言"])
    assert store.execute(identity, read).records[0].content == "使用中文"
    foreign = MemoryIdentity(
        bot_id="10009", user_id="10002", group_id="10003", session_id="qq_" + "a" * 32
    )
    assert store.execute(foreign, read).records == ()
    assert store.admin_get(created.id).content == "使用中文"
    assert store.admin_list(tag="语言").items[0].id == created.id
    assert store.admin_tags()[0].count == 1


async def test_concurrent_same_key_reuses_one_fact_and_revision(managed_memory):
    """同源同文并发请求均成功复用原事实，只生成一条当前记录和一个创建版本。"""
    store, _ = managed_memory
    body = MemoryCreate(scope="global", bot_id="10001", key="规则", content="原文", tags=["主题"])
    results = await asyncio.gather(
        *(asyncio.to_thread(store.admin_create, body) for _ in range(4)), return_exceptions=True
    )
    assert all(not isinstance(value, Exception) for value in results)
    assert len({value.id for value in results}) == 1
    assert {value.revision for value in results} == {1}
    assert len(store.admin_list().items) == 1
    assert len(store.admin_revisions(results[0].id).items) == 1


async def test_concurrent_different_keys_cannot_exceed_scope_capacity(managed_memory):
    """最后一个名额由同作用域事务独占领取，不同事实键也不能越过 256 条限制。"""
    from agent_bot.integrations.tagged_memory_store import TaggedMemoryCapacityError

    store, _ = managed_memory
    for index in range(255):
        store.admin_create(MemoryCreate(
            scope="global", bot_id="10001", key=f"old-{index}", content=f"已有事实{index}",
            tags=["主题"],
        ))
    results = await asyncio.gather(*(
        asyncio.to_thread(store.admin_create, MemoryCreate(
            scope="global", bot_id="10001", key=f"new-{index}", content=f"并发新增事实{index}",
            tags=["主题"],
        )) for index in range(8)
    ), return_exceptions=True)
    assert sum(not isinstance(item, Exception) for item in results) == 1
    assert sum(isinstance(item, TaggedMemoryCapacityError) for item in results) == 7
    assert store.admin_scopes()[0].count == 256


def test_admin_deleted_memory_retains_history(managed_memory):
    """删除只隐藏原行，同键即使删除也不恢复，已有作用域可继续新增另一键。"""
    store, params = managed_memory
    body = MemoryCreate(
        scope="user", bot_id="10001", target="10002", key="偏好", content="原文", tags=["原标签"]
    )
    item = store.admin_create(body)
    store.admin_delete(item.id, expected_revision=item.revision)
    assert not store.admin_list().items
    assert store.admin_tags() == ()
    with pytest.raises(KnowledgeError) as error:
        store.admin_get(item.id)
    assert error.value.status_code == 404
    with pytest.raises(RuntimeError, match="记忆键已存在"):
        store.admin_create(body)
    with sqlite3.connect(params.sqlite_path) as db:
        assert db.execute("SELECT content,is_deleted FROM chat_tagged_memory").fetchone() == (
            "原文",
            1,
        )
        assert db.execute("SELECT tags_json FROM chat_tagged_memory").fetchone() == ('["原标签"]',)
    second = store.admin_create(
        MemoryCreate(
            scope="user", scope_id=item.scope_id, key="新偏好", content="新文", tags=["新标签"]
        )
    )
    assert second.id != item.id
    assert store.admin_scopes()[0].count == 1
