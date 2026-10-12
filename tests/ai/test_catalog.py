import pytest

from agent_bot.ai.contracts import ModelCreate, ProviderCreate, ProviderUpdate
from agent_bot.ai.errors import AiError


async def seed(catalog):
    return await catalog.create_provider(
        ProviderCreate(
            name="test",
            protocol="ollama",
            base_url="http://localhost",
            enabled=True,
        )
    )


async def test_catalog_cas_and_model_reference(catalog):
    provider = await seed(catalog)
    model = await catalog.create_model(
        ModelCreate(
            alias="local",
            upstream_model="Qwen/X",
            provider_id=provider.id,
            enabled=True,
        )
    )
    with pytest.raises(AiError) as error:
        await catalog.delete_provider(provider.id, 1)
    assert error.value.status_code == 409
    patch = ProviderUpdate(expected_revision=1, enabled=False)
    assert (await catalog.update_provider(provider.id, patch)).revision == 2
    with pytest.raises(AiError) as error:
        await catalog.update_provider(provider.id, patch)
    assert error.value.status_code == 409
    with pytest.raises(AiError, match="AI_PROVIDER_DISABLED"):
        await catalog.resolve(model.alias)


async def test_case_sensitive_alias_and_snapshot(catalog):
    provider = await seed(catalog)
    for alias in ["Qwen/X", "qwen/x"]:
        await catalog.create_model(
            ModelCreate(
                alias=alias,
                upstream_model="exact/" + alias,
                provider_id=provider.id,
                enabled=True,
            )
        )
    snapshot = await catalog.resolve("Qwen/X")
    assert snapshot.model.upstream_model == "exact/Qwen/X"
    assert (await catalog.resolve("qwen/x")).model.upstream_model == "exact/qwen/x"
    await catalog.update_provider(provider.id, ProviderUpdate(expected_revision=1, enabled=False))
    assert snapshot.provider.enabled
    first = await catalog.list_models(limit=1)
    second = await catalog.list_models(limit=1, cursor=first.next_cursor)
    assert [first.items[0].alias, second.items[0].alias] == ["Qwen/X", "qwen/x"]


async def test_invalid_patch_and_uninitialized(catalog, ai_database):
    provider = await seed(catalog)
    with pytest.raises(ValueError):
        await catalog.update_provider(
            provider.id, ProviderUpdate(expected_revision=1, base_url=None)
        )
    ai_database.connection.execute("DROP TABLE agent_bot_ai_session")
    assert not (await catalog.readiness()).initialized
    with pytest.raises(AiError):
        await catalog.list_providers(limit=101)


async def test_schema_validation_and_config_timestamps(catalog, ai_database):
    provider = await seed(catalog)
    assert provider.created_at is not None and provider.updated_at is not None
    ai_database.connection.execute("ALTER TABLE agent_bot_ai_provider DROP COLUMN description")
    assert not (await catalog.readiness()).initialized
