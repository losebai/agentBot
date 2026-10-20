"""根据登记协议读取上游模型列表，不猜测端点或模型能力。"""

from pydantic import ValidationError

from agent_bot.ai.contracts import ModelCreate, UpstreamModels
from agent_bot.ai.credentials import CredentialResolver
from agent_bot.ai.errors import AiError


async def discover_models(provider, transport) -> UpstreamModels:
    """地址、密钥和结果版本来自同一个供应商快照。"""
    credential = CredentialResolver().resolve(provider)
    headers = {"Accept": "application/json"}
    if credential is not None:
        headers["Authorization"] = f"Bearer {credential.get_secret_value()}"
    ollama = provider.protocol == "ollama"
    path = "/api/tags" if ollama else "/models"
    value = await transport.get_json(provider.base_url + path, headers=headers, timeout_seconds=15)
    items = value.get("models" if ollama else "data") if isinstance(value, dict) else None
    if not isinstance(items, list) or len(items) > 2000:
        raise AiError("AI_MODEL_LIST_INVALID", status_code=502)
    models = []
    seen = set()
    for item in items:
        name = item.get("name" if ollama else "id") if isinstance(item, dict) else None
        try:
            ModelCreate(upstream_model=name, provider_id=provider.id)
        except ValidationError as error:
            raise AiError("AI_MODEL_LIST_INVALID", status_code=502) from error
        if name not in seen:
            models.append(name)
            seen.add(name)
    return UpstreamModels(provider_id=provider.id, revision=provider.revision, models=tuple(models))
