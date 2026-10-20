"""按显式目录协议批量生成向量，不切换模型或泄漏供应商凭据。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass

import httpx

from agent_bot.ai.catalog import AiCatalog

MAX_EMBEDDING_TEXTS = 1024
MAX_EMBEDDING_BYTES = 1024 * 1024
HTTP_BATCH_TEXTS = 128
HTTP_BATCH_BYTES = 96 * 1024


class EmbeddingError(RuntimeError):
    """可公开返回的固定向量服务错误，不包含上游响应和密钥。"""


def normalized_vector(value: object) -> tuple[float, ...]:
    """验证有限非零数值向量，用稳定范数归一化以便直接计算余弦。"""
    if not isinstance(value, (tuple, list)) or not value:
        raise EmbeddingError("Embedding 返回了无效向量")
    if any(type(number) not in (int, float) for number in value):
        raise EmbeddingError("Embedding 向量必须包含有限数值")
    try:
        vector = tuple(float(number) for number in value)
    except (ValueError, OverflowError) as error:
        raise EmbeddingError("Embedding 向量必须包含有限数值") from error
    if not all(math.isfinite(number) for number in vector):
        raise EmbeddingError("Embedding 向量必须包含有限数值")
    norm = math.hypot(*vector)
    if not math.isfinite(norm) or norm == 0:
        raise EmbeddingError("Embedding 返回了零向量或无效范数")
    return tuple(number / norm for number in vector)


@dataclass(frozen=True)
class EmbeddingBatch:
    """同一目录版本生成的等维单位向量，禁止跨模型混用。"""

    model_key: str
    vectors: tuple[tuple[float, ...], ...]

    def __post_init__(self) -> None:
        """缓存与 HTTP 结果共用验证，避免异常向量污染持久化状态。"""
        if not self.model_key or len(self.model_key) > 200:
            raise EmbeddingError("Embedding 模型版本标识无效")
        vectors = tuple(normalized_vector(vector) for vector in self.vectors)
        if len({len(vector) for vector in vectors}) > 1:
            raise EmbeddingError("Embedding 返回的向量维度不一致")
        object.__setattr__(self, "vectors", vectors)


class EmbeddingClient:
    """只通过数据库模型目录选取显式模型，HTTP 连接由单次调用释放。"""

    def __init__(self, catalog: AiCatalog, model_reference: str, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        """允许测试注入 HTTP transport，不引入其他配置或隐式模型。"""
        self._catalog = catalog
        self._model_reference = model_reference
        self._transport = transport

    async def embed(self, texts: tuple[str, ...]) -> EmbeddingBatch:
        """固定一次目录快照，按明确的条数及字节预算分批，不重试失败请求。"""
        if not self._model_reference:
            raise EmbeddingError("请先配置记忆 Embedding 模型")
        if not texts or any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("Embedding 输入不能为空")
        sizes = [len(text.encode("utf-8")) for text in texts]
        if len(texts) > MAX_EMBEDDING_TEXTS or sum(sizes) > MAX_EMBEDDING_BYTES:
            raise EmbeddingError("Embedding 批量输入超出本次请求预算")
        if max(sizes) > HTTP_BATCH_BYTES:
            raise EmbeddingError("Embedding 单条输入超出本次请求预算")
        resolved = await self._catalog.resolve(self._model_reference)
        provider, model = resolved.provider, resolved.model
        if not provider.credential_available:
            raise EmbeddingError("Embedding 供应商凭据不可用")
        if provider.protocol not in {"openai-chat", "openai-responses", "ollama"}:
            raise EmbeddingError("Embedding 供应商协议不受支持")
        # 目录版本变化使缓存自然失效；密钥不参与公开标识。
        descriptor = (provider.id, provider.revision, provider.protocol, provider.base_url,
                      model.id, model.revision, model.upstream_model)
        model_key = hashlib.sha256(json.dumps(descriptor).encode()).hexdigest()
        headers = {}
        if provider.credential is not None:
            headers["Authorization"] = f"Bearer {provider.credential.get_secret_value()}"
        ollama = provider.protocol == "ollama"
        url = provider.base_url.rstrip("/") + ("/api/embed" if ollama else "/embeddings")
        vectors = []
        async with httpx.AsyncClient(transport=self._transport, timeout=30) as client:
            offset = 0
            while offset < len(texts):
                end, byte_count = offset, 0
                while (end < len(texts) and end - offset < HTTP_BATCH_TEXTS
                       and byte_count + sizes[end] <= HTTP_BATCH_BYTES):
                    byte_count += sizes[end]
                    end += 1
                payload = {"model": model.upstream_model, "input": list(texts[offset:end])}
                payload.update({"truncate": False} if ollama else {"encoding_format": "float"})
                try:
                    response = await client.post(url, json=payload, headers=headers)
                    response.raise_for_status()
                    body = response.json()
                    if ollama:
                        values = body["embeddings"]
                    else:
                        items = body["data"]
                        if (not isinstance(items, list)
                                or any(type(item["index"]) is not int for item in items)
                                or sorted(item["index"] for item in items) != list(
                                    range(end - offset))):
                            raise EmbeddingError("Embedding 返回了无效输入索引")
                        values = [item["embedding"] for item in sorted(
                            items, key=lambda item: item["index"])]
                    if not isinstance(values, list) or len(values) != end - offset:
                        raise EmbeddingError("Embedding 返回的向量条数不一致")
                    vectors.extend(values)
                except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
                    raise EmbeddingError("Embedding 服务请求或响应失败") from error
                offset = end
        return EmbeddingBatch(model_key, tuple(vectors))
