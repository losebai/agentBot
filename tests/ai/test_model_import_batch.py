"""模型同步批量 SQL 保留原始身份与事务边界，不随模型数量逐项往返。"""

import pytest

from agent_bot.ai.contracts import ModelImportItem, ModelImportRequest, ProviderCreate
from ai.fake_database import Cursor


@pytest.mark.parametrize("size", [1, 100])
async def test_model_import_has_bounded_queries_and_preserves_order(catalog, monkeypatch, size):
    """首次同步与重复同步均为固定数量操作，原始大小写和路径前缀不被改写。"""
    provider = await catalog.create_provider(ProviderCreate(
        name="batch", protocol="ollama", base_url="http://offline.invalid",
    ))
    statements = []
    execute = Cursor.execute

    def record(self, sql, parameters=()):
        """统计真实替身 SQL 次数，不把网络延迟或实际生产库引入回归。"""
        statements.append(sql)
        return execute(self, sql, parameters)

    monkeypatch.setattr(Cursor, "execute", record)
    names = [f"batch/Model-{index}" for index in reversed(range(size))]
    body = ModelImportRequest(
        expected_revision=provider.revision,
        models=tuple(ModelImportItem(upstream_model=name) for name in names),
    )
    result = await catalog.import_models(provider.id, body)
    assert [item.upstream_model for item in result.imported] == names
    assert len({item.id for item in result.imported}) == size
    assert all(item.provider_name == "batch" and not item.enabled for item in result.imported)
    assert len(statements) == 6
    statements.clear()
    repeated = await catalog.import_models(provider.id, body)
    assert repeated.imported == () and list(repeated.skipped) == names
    assert len(statements) == 3
