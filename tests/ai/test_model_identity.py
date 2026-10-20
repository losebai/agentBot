"""验证稳定模型引用与供应商联合身份，所有数据库均为离线替身。"""

import pytest

from agent_bot.ai.contracts import (
    ModelCreate,
    ModelImportItem,
    ModelImportRequest,
    ModelUpdate,
    ModelView,
    ProviderCreate,
    ProviderUpdate,
)
from agent_bot.ai.errors import AiError


async def provider(catalog, name):
    """建立允许调用的本地供应商目录记录。"""
    return await catalog.create_provider(
        ProviderCreate(name=name, protocol="ollama", base_url="http://localhost", enabled=True)
    )


async def test_import_same_upstream_across_providers_without_alias(catalog):
    """无别名导入按供应商隔离，相同供应商重复导入保持原 ID。"""
    first, second = await provider(catalog, "first"), await provider(catalog, "second")
    body = ModelImportRequest(
        expected_revision=1,
        models=(ModelImportItem(upstream_model="shared"), ModelImportItem(upstream_model="other")),
    )
    one = await catalog.import_models(first.id, body)
    two = await catalog.import_models(second.id, body)
    assert len(one.imported) == len(two.imported) == 2
    assert one.imported[0].id != two.imported[0].id
    assert all(item.alias is None for item in (*one.imported, *two.imported))
    again = await catalog.import_models(first.id, body)
    assert again.imported == ()
    assert again.skipped == ("shared", "other")
    listed = (await catalog.list_models()).items
    assert [item.provider_name for item in listed] == ["first", "first", "second", "second"]
    assert [item.reference for item in listed] == ["model:1", "model:2", "model:3", "model:4"]
    with pytest.raises(AiError, match="AI_MODEL_DISABLED"):
        await catalog.resolve("model:3")
    await catalog.update_model(1, ModelUpdate(expected_revision=1, enabled=True))
    await catalog.update_model(3, ModelUpdate(expected_revision=1, enabled=True))
    assert (await catalog.resolve("model:1")).provider.id == first.id
    assert (await catalog.resolve("model:3")).provider.id == second.id


async def test_legacy_client_alias_conflicts_remain_explicit(catalog):
    """旧客户端的显式别名冲突仍报错，失败整批回滚并可明确请求跳过。"""
    first, second = await provider(catalog, "first"), await provider(catalog, "second")
    await catalog.create_model(
        ModelCreate(alias="old", upstream_model="registered", provider_id=first.id)
    )
    body = ModelImportRequest(
        expected_revision=1,
        models=(
            ModelImportItem(upstream_model="new"),
            ModelImportItem(alias="old", upstream_model="conflicting"),
        ),
    )
    with pytest.raises(AiError, match="AI_MODEL_ALIAS_CONFLICT"):
        await catalog.import_models(second.id, body)
    assert (await catalog.list_models(provider_id=second.id)).items == []
    skipped = await catalog.import_models(
        second.id, body.model_copy(update={"skip_alias_conflicts": True})
    )
    assert [item.upstream_model for item in skipped.imported] == ["new"]
    assert skipped.imported[0].alias is None
    assert skipped.conflicts == (body.models[1],)


async def test_canonical_reference_survives_provider_rename_and_legacy_alias(catalog):
    """记录 ID 固定真实供应商；供应商改名不改变引用，旧别名继续可读。"""
    source = await provider(catalog, "before")
    selected = await catalog.create_model(
        ModelCreate(alias="legacy", upstream_model="exact", provider_id=source.id, enabled=True)
    )
    assert selected.reference == "model:1"
    assert selected.provider_name == "before"
    await catalog.update_provider(source.id, ProviderUpdate(expected_revision=1, name="after"))
    stable = await catalog.resolve("model:1")
    assert stable.model.id == selected.id
    assert stable.model.provider_name == "after"
    assert (await catalog.resolve("legacy")).model.id == selected.id
    with pytest.raises(AiError, match="AI_MODEL_NOT_FOUND"):
        await catalog.resolve("model:999")


async def test_reference_rejects_legacy_alias_collision(catalog):
    """旧别名与另一个记录的规范引用重合时不得静默路由。"""
    source = await provider(catalog, "source")
    await catalog.create_model(
        ModelCreate(alias="first", upstream_model="first", provider_id=source.id, enabled=True)
    )
    await catalog.create_model(
        ModelCreate(alias="model:1", upstream_model="second", provider_id=source.id, enabled=True)
    )
    with pytest.raises(AiError, match="AI_MODEL_REFERENCE_AMBIGUOUS"):
        await catalog.resolve("model:1")


@pytest.mark.parametrize("value", ["model:0", "model:-1", "model:01", "model: 1", "model:1:2"])
async def test_reference_rejects_noncanonical_prefix(catalog, value):
    """保留前缀格式错误必须明确失败，不能作为旧别名绕过校验。"""
    with pytest.raises(AiError, match="AI_MODEL_REFERENCE_INVALID"):
        await catalog.resolve(value)


def test_reference_helpers_enforce_positive_canonical_id():
    """共享助手区分历史名称与严格的持久 ID 编码。"""
    from agent_bot.ai.model_reference import model_reference, parse_model_reference

    assert model_reference(12) == "model:12"
    assert parse_model_reference("model:12") == 12
    assert parse_model_reference("legacy") is None
    for value in ("model:0", "model:-1", "model:01", "model:1 ", "model:\uff11", "model:"):
        with pytest.raises(ValueError):
            parse_model_reference(value)
    for value in (0, -1, True):
        with pytest.raises(ValueError):
            model_reference(value)


def test_model_snapshot_roundtrip_and_legacy_missing_provider_name():
    """序列化引用不能改变主键身份，旧快照缺少供应商名仍可读取。"""
    snapshot = ModelView(
        id=7, revision=1, upstream_model="exact", provider_id=3,
    )
    assert snapshot.provider_name == ""
    restored = ModelView.model_validate(snapshot.model_dump())
    assert restored.reference == "model:7"
    with pytest.raises(ValueError):
        ModelView.model_validate({**snapshot.model_dump(), "reference": "model:8"})
