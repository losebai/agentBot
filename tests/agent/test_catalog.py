import pytest

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentCreate, AgentRequest, AgentUpdate, SkillCreate
from agent_bot.ai.contracts import ModelUpdate, ProviderUpdate
from agent_bot.ai.errors import AiError
from ai.mysql_identifiers import reject_unquoted_usage


async def test_crud_cas_and_references(agent_database, agent_params, seeded_model):
    ai, _, model = seeded_model
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    skill = await catalog.create_skill(SkillCreate(name="事实", system_prompt="Use evidence"))
    agent = await catalog.create_agent(
        AgentCreate(code="helper", name="助手", model_id=model.id, skill_ids=(skill.id,))
    )
    with pytest.raises(AiError) as conflict:
        await catalog.delete_skill(skill.id, skill.revision)
    assert conflict.value.status_code == 409
    changed = await catalog.update_agent(agent.id, AgentUpdate(expected_revision=1, skill_ids=()))
    assert changed.revision == 2 and changed.skill_ids == ()
    with pytest.raises(AiError) as stale:
        await catalog.update_agent(agent.id, AgentUpdate(expected_revision=1, name="stale"))
    assert stale.value.status_code == 409
    disabled = await ai.update_model(model.id, ModelUpdate(expected_revision=1, enabled=False))
    with pytest.raises(AiError) as referenced:
        await ai.delete_model(model.id, disabled.revision)
    assert referenced.value.status_code == 409
    await catalog.delete_agent(agent.id, 2)
    await catalog.delete_skill(skill.id, 1)
    assert (await catalog.list_agents()).items == []


@pytest.mark.parametrize("limit", [1, 2])
async def test_current_enabled_agent_is_first_without_skipping_other_pages(
    agent_database, agent_params, seeded_model, limit,
):
    """实际选中的角色即使在后页也置顶，其他已启用角色不能抢占且翻页不能漏项重复。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    for code in ("first", "second", "current", "last"):
        await catalog.create_agent(AgentCreate(code=code, name=code,
                                               model_id=seeded_model[2].id, enabled=True))
    page = await catalog.list_agents(limit=limit, preferred_code="current")
    codes = [item.code for item in page.items]
    assert codes[0] == "current" and len(page.items) == limit
    while page.next_cursor is not None:
        # 切换运行配置不能让已经显示过的置顶项再次出现在当前分页遍历中。
        page = await catalog.list_agents(limit=limit, cursor=page.next_cursor,
                                          preferred_code="last")
        assert len(page.items) <= limit
        codes.extend(item.code for item in page.items)
    assert codes == ["current", "first", "second", "last"]


@pytest.mark.parametrize("code", ["missing", "stopped", None])
async def test_missing_or_disabled_current_agent_preserves_original_order(
    agent_database, agent_params, seeded_model, code,
):
    """未配置、已停用或不存在的预设没有运行角色，目录仍可按原顺序维护。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    for name, enabled in (("first", True), ("stopped", False)):
        await catalog.create_agent(AgentCreate(code=name, name=name,
                                               model_id=seeded_model[2].id, enabled=enabled))
    result = await catalog.list_agents(preferred_code=code)
    assert [item.code for item in result.items] == ["first", "stopped"]


@pytest.mark.parametrize("cursor", ["p:0:1", "p:1:-1", "p:1:", "p:1:1 OR 1=1"])
async def test_pinned_agent_cursor_rejects_invalid_values(agent_database, agent_params, cursor):
    """置顶分页只接受有界数字身份，不能让非法游标进入SQL。"""
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    with pytest.raises(AiError) as rejected:
        await catalog.list_agents(cursor=cursor)
    assert rejected.value.code == "AGENT_INVALID_PAGE"


async def test_snapshot_survives_later_edits(agent_database, agent_params, seeded_model):
    ai, provider, model = seeded_model
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    agent = await catalog.create_agent(
        AgentCreate(code="helper", name="助手", model_id=model.id, enabled=True)
    )
    request = AgentRequest(agent_code=agent.code, prompt="hello", session_id="s", request_id="r")
    snapshot = await catalog.snapshot(request)
    await ai.update_provider(
        provider.id, ProviderUpdate(expected_revision=1, base_url="https://new.invalid/v1")
    )
    assert snapshot.resolved.provider.base_url == "https://original.invalid/v1"
    assert (await catalog.snapshot(request)).resolved.provider.base_url == "https://new.invalid/v1"
    for cursor in ["-1", "1 OR 1=1"]:
        with pytest.raises(AiError):
            await catalog.list_agents(cursor=cursor)


async def test_missing_tables_leave_plain_catalog_usable(agent_params):
    from ai.fake_database import FakeDatabase

    db = FakeDatabase()
    try:
        ready = await AgentCatalog(agent_params, connect=db.connect).readiness()
        assert not ready.initialized
        from agent_bot.ai.catalog import AiCatalog

        assert (await AiCatalog(agent_params, connect=db.connect).readiness()).initialized
    finally:
        db.close()


@pytest.mark.parametrize("mysql_semantics", [False, True], ids=["sqlite", "mysql"])
async def test_readiness_accepts_reserved_usage_column(
    agent_database, agent_params, monkeypatch, mysql_semantics
):
    """Agent 记录表存在时，usage 保留字不会导致健康检查误报。"""
    if mysql_semantics:
        reject_unquoted_usage(monkeypatch)
    ready = await AgentCatalog(agent_params, connect=agent_database.connect).readiness()
    assert ready.initialized
    assert ready.code is None
