"""验证能力原子更新、可选表兼容和一致的请求快照。"""

import pymysql
import pytest
from pydantic import ValidationError

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentRequest
from agent_bot.ai.contracts import ModelCreate, ModelMediaCapabilities, ModelUpdate, ProviderUpdate
from agent_bot.ai.errors import AiError
from agent_bot.config import RuntimeConfig
from agent_bot.settings import runtime_config_to_plain_values


async def test_plain_catalog_works_without_media_tables(seeded_model):
    """缺少媒体表不阻断旧创建、编辑、分页及普通文本就绪检查。"""
    catalog, _, model = seeded_model
    assert model.media_initialized is False
    assert not model.media.vision_enabled
    updated = await catalog.update_model(model.id, ModelUpdate(expected_revision=1, alias="plain"))
    assert updated.alias == "plain" and not updated.media_initialized
    assert (await catalog.list_models()).items == [updated]
    assert (await catalog.readiness(updated.alias)).initialized
    with pytest.raises(AiError, match="MEDIA_NOT_INITIALIZED"):
        await catalog.update_model(model.id, ModelUpdate(expected_revision=2, media={}))
    assert (await catalog.resolve("plain")).model.revision == 2


async def test_media_update_uses_model_revision(media_model):
    """能力与名称共享版本，省略保留，显式对象完整替换，空值拒绝。"""
    catalog, _, model = media_model
    assert model.media_initialized and model.media.audio_chat_enabled
    changed = await catalog.update_model(model.id, ModelUpdate(expected_revision=2, alias="new"))
    assert changed.media == model.media
    with pytest.raises(AiError) as conflict:
        await catalog.update_model(model.id, ModelUpdate(expected_revision=2, media={}))
    assert conflict.value.status_code == 409
    changed = await catalog.update_model(model.id, ModelUpdate(expected_revision=3, media={}))
    assert changed.media == ModelMediaCapabilities()
    with pytest.raises(ValidationError):
        ModelUpdate(expected_revision=4, media=None)


async def test_provider_protocol_change_rejects_incompatible_media(media_model):
    """协议更新和新模型均不能把生成能力绑定到 Ollama。"""
    catalog, provider, model = media_model
    with pytest.raises(AiError, match="MEDIA_PROTOCOL_UNSUPPORTED"):
        await catalog.update_provider(
            provider.id, ProviderUpdate(expected_revision=1, protocol="ollama")
        )
    assert (await catalog.resolve(model.alias)).provider.revision == 1
    await catalog.update_model(
        model.id, ModelUpdate(expected_revision=2, media={"vision_enabled": True})
    )
    await catalog.update_provider(
        provider.id, ProviderUpdate(expected_revision=1, protocol="ollama")
    )
    with pytest.raises(AiError, match="MEDIA_PROTOCOL_UNSUPPORTED"):
        await catalog.create_model(
            ModelCreate(
                alias="bad",
                upstream_model="bad",
                provider_id=provider.id,
                media={"audio_chat_enabled": True},
            )
        )


async def test_snapshot_keeps_previous_media_capability(media_model, media_database, agent_params):
    """管理员后续修改能力不改变已开始的 Agent 请求。"""
    catalog, _, model = media_model
    agent = AgentCatalog(agent_params, connect=media_database.connect)
    request = AgentRequest(model=model.alias, prompt="图中是什么", session_id="s", request_id="r")
    snapshot = await agent.snapshot(request)
    await catalog.update_model(model.id, ModelUpdate(expected_revision=2, media={}))
    assert snapshot.resolved.model.media.vision_enabled
    assert not (await agent.snapshot(request)).resolved.model.media.vision_enabled
    assert (await catalog.list_models()).items[0].media_initialized


def test_media_backend_roundtrip():
    """媒体后端采用既有运行配置序列化，不影响默认 Java 路由。"""
    credentials = dict(onebot_v11_access_token="x" * 40, dispatch_chat_session_secret="y" * 40)
    assert RuntimeConfig(**credentials).dispatch_chat_media_backend == "java"
    values = runtime_config_to_plain_values(
        RuntimeConfig(**credentials, dispatch_chat_media_backend="python")
    )
    assert RuntimeConfig.model_validate(values).dispatch_chat_media_backend == "python"
    with pytest.raises(ValidationError):
        RuntimeConfig(**credentials, dispatch_chat_media_backend="automatic")


def test_media_reader_does_not_hide_other_database_failures():
    """只有表不存在可以降级，数据库故障必须继续上报。"""
    from unittest.mock import MagicMock

    from agent_bot.media.catalog import read_model_media

    connection = MagicMock()
    connection.cursor.return_value.__enter__.return_value.execute.side_effect = (
        pymysql.OperationalError(2006, "offline unavailable")
    )
    with pytest.raises(pymysql.OperationalError):
        read_model_media(connection, 1)
