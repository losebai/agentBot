"""模型稳定身份贯穿Agent快照和执行归档，不改变模型调用或授权边界。"""

from agent_bot.agent.catalog import AgentCatalog
from agent_bot.agent.contracts import AgentRequest, AgentSnapshot, ExecutionAuthority
from agent_bot.agent.model_routing import AgentModelRouter
from agent_bot.agent.records import AgentRunStore
from agent_bot.ai.contracts import ResolvedModel
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.errors import AiError
from agent_bot.models import LlmRequestOptions

from .test_service import request, service_for


async def test_agent_without_preset_resolves_a_canonical_model_reference(
    agent_database, agent_params, seeded_model,
):
    """直接调用使用模型记录身份，不能将规范引用当成上游名称或旧别名。"""
    _, provider, model = seeded_model
    catalog = AgentCatalog(agent_params, connect=agent_database.connect)
    snapshot = await catalog.snapshot(AgentRequest(
        model=f"model:{model.id}", prompt="hello", session_id="s", request_id="r",
    ))
    assert snapshot.resolved.model.id == model.id
    assert snapshot.resolved.provider.id == provider.id


async def test_agent_audit_routes_keep_the_resolved_identity(
    agent_database, agent_params, seeded_model,
):
    """即使旧请求仍携带别名，新执行快照也记录稳定ID，保留准确的模型路由。"""
    captured = []

    class ObservedStore(AgentRunStore):
        """记录真实持久化入口收到的归档，不替代其数据库事务。"""

        async def begin(self, start):
            """保留原始归档以检查路由身份，再执行真实离线存储。"""
            captured.append(start)
            return await super().begin(start)

    records = ObservedStore(agent_params, connect=agent_database.connect)
    service = service_for(agent_database, agent_params, records=records)
    result = await service.complete(
        request(), ExecutionAuthority(entry="admin", allowed_tools=("lookup",))
    )
    assert result.content == "最终回答"
    assert captured[0].request_context["model_routes"] == {
        "primary": f"model:{seeded_model[2].id}", "fallback": None,
    }


async def test_model_forwarding_names_the_provider_and_upstream_model(seeded_model):
    """两个供应商提供同名模型时，接管说明也能让用户分清实际目标。"""
    from types import SimpleNamespace

    _, provider, model = seeded_model
    fallback_provider = provider.model_copy(update={"id": 32, "name": "备用供应商"})
    fallback_model = model.model_copy(update={"id": 72, "provider_id": 32, "alias": None})
    snapshot = AgentSnapshot(
        resolved=ResolvedModel(model=model, provider=provider),
        fallback=ResolvedModel(model=fallback_model, provider=fallback_provider),
    )
    router = AgentModelRouter(
        snapshot, {"openai-chat": SimpleNamespace(validate_options=lambda _: None)},
        CredentialResolver(), LlmRequestOptions(), has_images=False, timeout_seconds=10,
    )
    warning = router.forward(AiError("AI_TIMEOUT", status_code=504))
    assert warning == "AI_TIMEOUT：local / exact/id 转交 备用供应商 / exact/id"
    assert router.resolved.model.id == 72
