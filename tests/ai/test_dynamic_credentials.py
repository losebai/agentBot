"""数据库凭据更新和执行快照不读取环境变量。"""

import pytest

from agent_bot.ai.contracts import ModelCreate, ProviderCreate, ProviderUpdate
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.errors import AiError


async def test_credential_replacement_clear_and_snapshot(catalog, monkeypatch):
    """普通字段更新保留密钥，替换只影响后续快照，清除必须显式提交。"""
    monkeypatch.setenv("AGENT_BOT_PROVIDER_TEST_KEY", "environment-must-not-be-read")
    provider = await catalog.create_provider(
        ProviderCreate(
            name="dynamic",
            protocol="openai-chat",
            base_url="https://one.invalid/v1",
            credential="database-first",
            enabled=True,
        )
    )
    model = await catalog.create_model(
        ModelCreate(
            alias="dynamic-model",
            upstream_model="same-model",
            provider_id=provider.id,
            enabled=True,
        )
    )
    first = await catalog.resolve(model.alias)
    assert CredentialResolver().resolve(first.provider).get_secret_value() == "database-first"
    assert "credential" not in provider.model_dump()
    assert "database-first" not in provider.model_dump_json()
    assert "database-first" not in repr(provider)
    await catalog.update_provider(
        provider.id, ProviderUpdate(expected_revision=1, description="更新说明")
    )
    assert (
        CredentialResolver()
        .resolve((await catalog.resolve(model.alias)).provider)
        .get_secret_value()
        == "database-first"
    )
    await catalog.update_provider(
        provider.id, ProviderUpdate(expected_revision=2, credential="database-second")
    )
    assert CredentialResolver().resolve(first.provider).get_secret_value() == "database-first"
    assert (
        CredentialResolver()
        .resolve((await catalog.resolve(model.alias)).provider)
        .get_secret_value()
        == "database-second"
    )
    await catalog.update_provider(provider.id, ProviderUpdate(expected_revision=3, credential=None))
    assert CredentialResolver().resolve((await catalog.resolve(model.alias)).provider) is None


async def test_migrated_required_credential_is_not_anonymous(catalog, ai_database):
    """旧引用待录入时阻止匿名访问，修改说明不能清除鉴权要求。"""
    provider = await catalog.create_provider(
        ProviderCreate(
            name="legacy",
            protocol="ollama",
            base_url="http://127.0.0.1:11434",
        )
    )
    ai_database.connection.execute(
        "UPDATE agent_bot_ai_provider SET credential_required=1 WHERE id=?",
        (provider.id,),
    )
    ai_database.connection.commit()
    updated = await catalog.update_provider(
        provider.id, ProviderUpdate(expected_revision=1, description="保留鉴权")
    )
    assert not updated.credential_available
    with pytest.raises(AiError, match="AI_CREDENTIAL_MISSING"):
        CredentialResolver().resolve(updated)


async def test_unmigrated_provider_schema_cannot_be_treated_as_anonymous(catalog, ai_database):
    """既有库缺少新字段时明确报迁移要求，不能由模型默认值绕过鉴权。"""
    provider = await catalog.create_provider(
        ProviderCreate(
            name="old-schema",
            protocol="ollama",
            base_url="http://localhost:11434",
        )
    )
    ai_database.connection.execute("ALTER TABLE agent_bot_ai_provider DROP COLUMN credential")
    ai_database.connection.execute(
        "ALTER TABLE agent_bot_ai_provider DROP COLUMN credential_required"
    )
    ai_database.connection.commit()
    with pytest.raises(AiError, match="AI_NOT_INITIALIZED"):
        await catalog.get_provider(provider.id, provider.revision)
    with pytest.raises(AiError, match="AI_NOT_INITIALIZED"):
        await catalog.list_providers()
