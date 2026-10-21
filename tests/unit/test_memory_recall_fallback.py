"""无向量模型或服务失败时，本地关键词检索保持权限和事实有效性。"""

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr
from test_tagged_memory import memory_store as _memory_store

from agent_bot.agent.contracts import ExecutionAuthority, ToolCall
from agent_bot.agent.tools.chat import ChatToolBridge
from agent_bot.ai.errors import AiError
from agent_bot.integrations.mcp import ChatMcpServer
from agent_bot.memory.embeddings import EmbeddingError
from agent_bot.services.chat_tools import ChatToolContext, build_chat_tool_registry
from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity, MemorySession
from agent_bot.services.tool_sessions import ToolSessionStore

memory_store = _memory_store


async def memory_bridge(memory_store, *, semantic=None):
    """创建仅授权记忆的真实桥接链，向量服务使用离线替身。"""
    identity = MemoryIdentity(bot_id="90001", user_id="10001", session_id="session")
    memory_store.execute(identity, MemoryArguments(
        action="save", scope="user", key="drawing", content="偏好粉色大耳朵形象",
        tags=("绘图",),
    ))
    memory_store.execute(MemoryIdentity(
        bot_id="90001", user_id="10002", session_id="foreign",
    ), MemoryArguments(action="save", scope="user", key="private",
                       content="其他用户的私密偏好", tags=("绘图",)))
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    memory = MemorySession(memory_store, identity, semantic_search=semantic)
    token = await sessions.create(ChatToolContext(
        session_id=identity.session_id, user_id=identity.user_id,
        allowed_tools=frozenset({"memory"}), memory_session=memory,
    ))
    return ChatToolBridge(ChatMcpServer(
        store=sessions, registry=build_chat_tool_registry(), allowed_hosts=["localhost:*"],
    )), ExecutionAuthority(
        entry="qq", allowed_tools=("memory",), chat_token=SecretStr(token),
    ), memory


async def test_unconfigured_semantic_recall_uses_keywords_then_allows_tag_read(memory_store):
    """query 无需额外模型即可检索，结果不扩大作用域，也不冒充向量结果。"""
    bridge, authority, _ = await memory_bridge(memory_store)
    result = await bridge.call(ToolCall(call_id="query", name="memory", arguments={
        "action": "recall", "scope": "all", "query": "桃桃画图偏好",
    }), authority)
    assert not result.is_error
    body = json.loads(result.content)
    assert body["mode"] == "lexical"
    assert body["fallback_reason"] == "embedding_not_configured"
    assert [record["content"] for record in body["records"]] == ["偏好粉色大耳朵形象"]
    result = await bridge.call(ToolCall(call_id="tags", name="memory", arguments={
        "action": "recall", "scope": "all", "tags": ["绘图"],
    }), authority)
    assert not result.is_error
    body = json.loads(result.content)
    assert body["mode"] == "tag"
    assert [record["content"] for record in body["records"]] == ["偏好粉色大耳朵形象"]


@pytest.mark.parametrize("configured", [False, True])
async def test_tool_description_exposes_current_recall_capability(memory_store, configured):
    """调用前声明兜底能力，模型可以按实际模式解释结果。"""
    bridge, authority, _ = await memory_bridge(
        memory_store, semantic=AsyncMock() if configured else None,
    )
    tools = await bridge.list_tools(authority)
    description = tools[0].description
    assert ("本请求优先语义检索" if configured else "本请求未配置 Embedding") in description
    assert "关键词兜底" in description
    assert "绘图" in description
    assert "其他用户的私密偏好" not in description


@pytest.mark.parametrize("failure", [EmbeddingError("private-key=secret"),
                                     AiError("AI_MODEL_NOT_FOUND")])
async def test_embedding_failure_uses_keywords_without_leaking_internal_error(
    memory_store, failure,
):
    """服务故障和模型目录失效均可兜底，不返回上游错误正文。"""
    search = SimpleNamespace(search=AsyncMock(side_effect=failure))
    bridge, authority, _ = await memory_bridge(memory_store, semantic=search)
    result = await bridge.call(ToolCall(call_id="query", name="memory", arguments={
        "action": "recall", "scope": "all", "query": "粉色 大耳朵",
    }), authority)
    assert not result.is_error
    body = json.loads(result.content)
    assert body["mode"] == "lexical" and body["fallback_reason"] == "embedding_unavailable"
    assert len(body["records"]) == 1
    assert "private-key" not in result.content
    search.search.assert_awaited_once()


async def test_local_query_with_no_match_returns_no_unrelated_memories(memory_store):
    """元信息分数不能把无词项命中的事实伪装成相关内容。"""
    bridge, authority, _ = await memory_bridge(memory_store)
    result = await bridge.call(ToolCall(call_id="query", name="memory", arguments={
        "action": "recall", "scope": "all", "query": "路由器固件",
    }), authority)
    assert not result.is_error
    assert json.loads(result.content)["records"] == []


async def test_semantic_budget_exhaustion_uses_local_search_without_extra_model_call(memory_store):
    """第三次查询只用本地检索，既保留功能也不突破每请求向量调用预算。"""
    async def found(query, candidates, **kwargs):
        """语义服务替身只返回传入的授权候选。"""
        return candidates[:1]

    search = SimpleNamespace(search=AsyncMock(side_effect=found))
    _, _, memory = await memory_bridge(memory_store, semantic=search)
    for _ in range(2):
        result = await memory.execute(MemoryArguments(action="recall", query="粉色 大耳朵"))
        assert result.mode == "semantic"
    result = await memory.execute(MemoryArguments(
        action="recall", scope="all", query="粉色 大耳朵",
    ))
    assert result.mode == "lexical" and result.fallback_reason == "semantic_budget_exhausted"
    assert len(result.records) == 1
    assert search.search.await_count == 2


async def test_fallback_rechecks_forgetting_after_embedding_failure(memory_store):
    """向量请求在途发生遗忘时，兜底也不能把旧候选重新带回上下文。"""
    async def forget_then_fail(query, candidates, **kwargs):
        """用真实事务模拟模型等待期间发生的明确遗忘。"""
        memory_store.admin_delete(candidates[0]["id"], expected_revision=candidates[0]["revision"])
        raise EmbeddingError("服务暂不可用")

    search = SimpleNamespace(search=AsyncMock(side_effect=forget_then_fail))
    _, _, memory = await memory_bridge(memory_store, semantic=search)
    result = await memory.execute(MemoryArguments(
        action="recall", scope="all", query="粉色 大耳朵",
    ))
    assert result.mode == "lexical"
    assert result.records == ()
    assert result.snapshot_changed


@pytest.mark.parametrize("failure", [asyncio.CancelledError(), RuntimeError("程序错误")])
async def test_cancellation_and_unknown_errors_do_not_trigger_fallback(memory_store, failure):
    """兜底只处理已知向量故障，不能把请求取消或程序错误伪装成成功。"""
    search = SimpleNamespace(search=AsyncMock(side_effect=failure))
    _, _, memory = await memory_bridge(memory_store, semantic=search)
    with pytest.raises(type(failure)):
        await memory.execute(MemoryArguments(action="recall", scope="all", query="粉色"))


async def test_embedding_failure_at_request_deadline_cannot_return_late_fallback(memory_store):
    """总期限已用尽时保留超时结果，兜底不能延长调用或返回迟到内容。"""
    async def expired(query, candidates, **kwargs):
        """模拟上游耗尽本请求剩余时间后失败。"""
        memory.deadline_at = time.monotonic() - 1
        raise EmbeddingError("本次记忆检索已超时")

    _, _, memory = await memory_bridge(
        memory_store, semantic=SimpleNamespace(search=AsyncMock(side_effect=expired)),
    )
    with pytest.raises(EmbeddingError, match="超时"):
        await memory.execute(MemoryArguments(action="recall", scope="all", query="粉色"))
