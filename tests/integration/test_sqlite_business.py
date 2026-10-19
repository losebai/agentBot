"""完整初始化后通过真实 SQLite 验证业务仓储和并发仲裁。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentCreate, AgentUpdate, SkillCreate
from agent_bot.ai.catalog import AiCatalog
from agent_bot.ai.contracts import ModelCreate, ProviderCreate, ProviderUpdate
from agent_bot.ai.errors import AiError
from agent_bot.database import database_connector, initialize_database
from agent_bot.group_members import GroupMemberStore
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.integrations.workflow_store import WorkflowStore
from agent_bot.jobs.contracts import JobCommand, JobScope
from agent_bot.jobs.store import JobStore
from agent_bot.memory_store import (
    GroupContextPolicy,
    MySqlRecentGroupContextStore,
)
from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity
from agent_bot.services.workflows import WorkflowDefinition
from agent_bot.session_context import SessionContextPolicy, SessionContextStore
from agent_bot.session_schema import SessionContextSchema
from agent_bot.settings import DatabaseParams


@pytest.fixture
def database(tmp_path):
    """隔离文件保证从空库开始，所有 SQL 都由生产适配执行。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "bot.db"))
    initialize_database(params)
    return params


async def test_catalog_credentials_case_sensitive_aliases_and_agent_relations(database):
    """别名区分大小写，密钥只供内部使用，关系删除与版本锁仍生效。"""
    connect = database_connector(database)
    catalog = AiCatalog(database, connect=connect)
    provider = await catalog.create_provider(
        ProviderCreate(
            name="本地",
            protocol="openai-chat",
            base_url="https://offline.invalid/v1",
            credential="offline-provider-secret",
            enabled=True,
        )
    )
    assert "offline-provider-secret" not in provider.model_dump_json()
    models = []
    for alias in ("Qwen/X", "qwen/x"):
        models.append(
            await catalog.create_model(
                ModelCreate(
                    alias=alias,
                    upstream_model=alias,
                    provider_id=provider.id,
                    enabled=True,
                )
            )
        )
    assert (await catalog.resolve("Qwen/X")).model.upstream_model == "Qwen/X"
    assert (await catalog.resolve("qwen/x")).model.upstream_model == "qwen/x"
    agents = AgentCatalog(database, connect=connect)
    skill = await agents.create_skill(SkillCreate(name="事实", system_prompt="Use evidence"))
    agent = await agents.create_agent(
        AgentCreate(
            code="helper",
            name="助手",
            model_id=models[0].id,
            skill_ids=(skill.id,),
        )
    )
    with pytest.raises(AiError):
        await agents.delete_skill(skill.id, 1)
    changed = await agents.update_agent(agent.id, AgentUpdate(expected_revision=1, skill_ids=()))
    assert changed.revision == 2 and changed.skill_ids == ()
    await agents.delete_agent(agent.id, 2)
    await agents.delete_skill(skill.id, 1)
    await catalog.update_provider(provider.id, ProviderUpdate(expected_revision=1, enabled=False))
    with pytest.raises(AiError, match="AI_PROVIDER_DISABLED"):
        await catalog.resolve("Qwen/X")


async def test_job_deduplication_and_single_due_claim(database):
    """并发创建不产生重复任务，并发领取只有一个执行者。"""
    now = [datetime(2026, 10, 13, tzinfo=UTC)]
    store = JobStore(database, connect=database_connector(database), clock=lambda: now[0])
    scope = JobScope(
        bot_id="10001",
        session_id="session",
        sender_id="10002",
        scene="group",
        group_id="10003",
        request_id="request",
        model="offline",
        system_prompt="可信提示",
    )
    command = JobCommand.model_validate({"action": "create", "prompt": "提醒", "delaySeconds": 1})
    results = await asyncio.gather(*(store.create(scope, command) for _ in range(4)))
    assert len({item.id for item in results}) == 1
    now[0] += timedelta(seconds=2)
    claims = await asyncio.gather(store.claim_due(), store.claim_due())
    assert sum(item is not None for item in claims) == 1
    await store.cancel(scope, results[0].id)
    await store.finish(results[0].id, result="迟到结果")
    assert (await store.get(scope, results[0].id)).status == "CANCELLED"


def test_memory_and_workflow_startup_validate_initialized_schema(database):
    """SQLite 启动不执行 MySQL DDL，记忆写入和重放使用真实约束。"""
    SessionContextSchema(database).validate()
    TaggedMemoryStore(database, secret="offline").initialize()
    workflows = WorkflowStore(database)
    workflows.prepare_for_startup()
    workflows.migrate()
    assert workflows.list_all() == ()
    store = SessionContextStore(
        database,
        policy=SessionContextPolicy(
            history_turns=2,
            max_history_chars=1000,
            memory_enabled=False,
            max_memory_chars=100,
        ),
    )
    store.commit_exchange("session", "你好", "回应", request_id="request")
    store.commit_exchange("session", "你好", "回应", request_id="request")
    assert [message.content for message in store.context("session").messages] == ["你好", "回应"]


async def test_group_member_upsert_preserves_one_member(database):
    """冲突更新与中文文本由 SQLite 正常保存，复用原权限与标识校验。"""
    store = GroupMemberStore(database)
    values = {"bot_id": "10001", "group_id": "10003", "group_name": "测试群"}
    await store.upsert_member(
        **values, member={"user_id": "10002", "nickname": "成员", "role": "member"}
    )
    await store.upsert_member(
        **values, member={"user_id": "10002", "nickname": "新名字", "role": "member"}
    )
    members = await store.list_members("10001", "10003")
    assert len(members) == 1 and members[0]["nickname"] == "新名字"


def test_tagged_memory_group_context_and_cleanup(database):
    """标签关联及过期清理使用生产查询，授权作用域不能串入其他机器人。"""
    tagged = TaggedMemoryStore(database, secret="offline")
    identity = MemoryIdentity(bot_id="10001", user_id="10002", session_id="session")
    tagged.execute(
        identity,
        MemoryArguments(
            action="save",
            scope="user",
            key="游戏",
            content="喜欢永劫",
            tags=["游戏"],
        ),
    )
    read = MemoryArguments(action="recall", scope="all", tags=["游戏"])
    assert tagged.execute(identity, read).records[0].content == "喜欢永劫"
    foreign = MemoryIdentity(bot_id="10009", user_id="10002", session_id="session")
    assert tagged.execute(foreign, read).records == ()
    now = [datetime(2026, 10, 13, tzinfo=UTC)]
    recent = MySqlRecentGroupContextStore(
        database,
        secret="offline",
        clock=lambda: now[0],
        policy=GroupContextPolicy(message_limit=2, max_chars=1000, ttl_seconds=60),
    )
    recent.record(
        bot_id="10001", group_id="10003", sender_id="10002", sender_name="成员", content="群聊内容"
    )
    assert "群聊内容" in recent.context(bot_id="10001", group_id="10003")
    now[0] += timedelta(seconds=61)
    assert recent.cleanup_expired() == 1
    assert recent.context(bot_id="10001", group_id="10003") is None


def test_workflow_import_deduplicates_and_retains_business_metadata(database):
    """重复同步由真实唯一索引去重，不覆盖管理员已保存的描述。"""
    store = WorkflowStore(database)
    definition = WorkflowDefinition(
        workflow_key="offline-flow",
        display_name="离线",
        source_file="flow.json",
        source_format="api",
        ui_json=None,
        api_json='{"node":{}}',
        content_sha256="a" * 64,
        positive_node_id="1",
        negative_node_id="2",
        output_node_id="3",
        seed_node_id="4",
    )
    assert store.upsert([definition]) == 1
    assert store.upsert([definition]) == 1
    records = store.list_all()
    assert len(records) == 1 and records[0].workflow_key == "offline-flow"


async def test_media_file_record_lifecycle_and_authorization(database, tmp_path):
    """SQLite 元数据与临时文件一致，媒体读取仍检查会话归属。"""
    from io import BytesIO

    from PIL import Image

    from agent_bot.media.contracts import MediaOwner
    from agent_bot.media.errors import MediaError
    from agent_bot.media.files import MediaFiles
    from agent_bot.media.store import MediaStore

    content = BytesIO()
    Image.new("RGB", (2, 2)).save(content, format="PNG")
    files = MediaFiles(tmp_path / "media")
    files.start()
    store = MediaStore(database, connect=database_connector(database), files=files)
    owner = MediaOwner(entry="admin", session_id="session")
    try:
        asset = await store.put(
            owner=owner, purpose="input", data=content.getvalue(), mime_type="image/png"
        )
        assert asset.status == "ready"
        assert await store.describe(asset.id, owner=owner) == asset
        with pytest.raises(MediaError):
            await store.describe(asset.id, owner=MediaOwner(entry="admin", session_id="other"))
    finally:
        await store.aclose()
