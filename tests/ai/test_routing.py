"""本地聊天入口复用模型目录、请求预算及错误分类。"""

import json

import httpx
import pytest

from agent_bot.ai.chat_errors import ChatAiRejectedError
from agent_bot.ai.routing import RoutedModelGuard, RoutingAiClient
from agent_bot.model_guard import ModelNotAvailableError
from agent_bot.models import ChatMessage, LlmRequestOptions

from .test_service import make_service


async def test_default_text_resolves_local_model(catalog, ai_params, ai_database):
    """没有后端配置仍可按本地别名完成对话，缺失模型在发请求前拒绝。"""
    calls = []

    def upstream(req):
        """隔离供应商网络，同时记录实际上游模型名。"""
        calls.append(json.loads(req.content))
        return httpx.Response(200, json={"message": {"content": "本地回复"}, "done": True})

    service = await make_service(catalog, ai_params, ai_database, upstream)
    router = RoutingAiClient(
        service, timeout_seconds=2, default_options=LlmRequestOptions(), mcp_services=()
    )
    try:
        args = dict(
            model="local", session_id="s", prompt="hello", request_id="r",
            messages=[ChatMessage(role="user", content="hello")],
            use_agent=False, use_web_search=False,
        )
        assert await router.complete(**args) == "本地回复"
        assert calls[0]["model"] == "exact/model"
        with pytest.raises(ChatAiRejectedError, match="AI_MODEL_NOT_FOUND"):
            await router.complete(**{**args, "model": "missing"})
        assert len(calls) == 1
    finally:
        await router.aclose()


async def test_model_guard_uses_local_catalog(catalog, ai_params, ai_database):
    """预检采用同一本地目录，不再读取旧服务的模型表。"""
    service = await make_service(catalog, ai_params, ai_database, lambda req: httpx.Response(200))
    try:
        guard = RoutedModelGuard(catalog)
        await guard.ensure_enabled("local")
        with pytest.raises(ModelNotAvailableError, match="AI_MODEL_NOT_FOUND"):
            await guard.ensure_enabled("missing")
    finally:
        await service.aclose()
