from __future__ import annotations

from collections.abc import Sequence
from unittest.mock import AsyncMock

import pytest

from agent_bot.model_router import ContentModelRouter
from agent_bot.models import ChatMessage, ChatToolName, LlmRequestOptions
from agent_bot.services.chat import ChatService
from agent_bot.state import ConversationStore


def model_router() -> ContentModelRouter:
    return ContentModelRouter(
        text_model="chat-model",
        vision_model="vision-model",
        image_model="image-model",
        audio_model="audio-model",
    )


class CapturingClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def complete(
        self,
        *,
        model: str,
        session_id: str,
        prompt: str,
        messages: Sequence[ChatMessage],
        request_id: str,
        user: str | None = None,
        image_urls: Sequence[str] = (),
        video_urls: Sequence[str] = (),
        use_agent: bool = True,
        use_web_search: bool = True,
        request_options: LlmRequestOptions | None = None,
        chat_tools: Sequence[ChatToolName] = (),
        tool_session_token: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "messages": list(messages),
                "request_id": request_id,
                "video_urls": tuple(video_urls),
                "use_agent": use_agent,
                "use_web_search": use_web_search,
                "chat_tools": tuple(chat_tools),
                "tool_session_token": tool_session_token,
            }
        )
        return "工具回复"


@pytest.mark.asyncio
@pytest.mark.parametrize("web_search_enabled", [True, False])
async def test_service_propagates_remote_tool_context_without_persisting_the_token(
    web_search_enabled: bool,
) -> None:
    """自然聊天固定使用 Agent，联网授权仍独立受开关控制。"""

    client = CapturingClient()
    store = ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60)
    service = ChatService(
        client=client,
        store=store,
        system_prompt="系统提示",
        max_input_chars=100,
        cooldown_seconds=0,
        max_concurrent_requests=2,
        model_router=model_router(),
        web_search_enabled=web_search_enabled,
    )
    token = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEFG"

    answer = await service.respond(
        "group:1",
        "查询战绩",
        delivery=_deliver,
        chat_tools=("naraka_query",),
        tool_session_token=token,
    )

    assert answer == "工具回复"
    assert client.calls[0]["chat_tools"] == ("naraka_query",)
    assert client.calls[0]["tool_session_token"] == token
    assert [message.history_request_id for message in store.messages("group:1")] == [
        client.calls[0]["request_id"], client.calls[0]["request_id"],
    ]
    assert client.calls[0]["use_agent"] is True
    assert client.calls[0]["use_web_search"] is web_search_enabled
    persisted = store.context("group:1")
    assert token not in "".join(message.content for message in persisted.messages)


@pytest.mark.asyncio
async def test_service_uses_agent_when_web_search_is_disabled() -> None:
    """关闭联网搜索也不能把自然聊天切回普通模型。"""

    client = CapturingClient()
    service = ChatService(
        client=client,
        store=ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60),
        system_prompt="系统提示",
        max_input_chars=100,
        cooldown_seconds=0,
        max_concurrent_requests=2,
        model_router=model_router(),
        web_search_enabled=False,
    )

    await service.ask("private:1", "查看当前窗口")

    assert client.calls[0]["use_agent"] is True
    assert client.calls[0]["use_web_search"] is False


async def _deliver(answer: str) -> str:
    return answer


@pytest.mark.parametrize("entry", ["ask", "respond", "generate_queued"])
async def test_natural_chat_always_uses_agent_without_tools(entry: str) -> None:
    """关闭联网且没有工具时，各自然聊天入口仍使用 Agent，专用指令保持普通调用。"""
    client = CapturingClient()
    guard = AsyncMock()
    service = ChatService(
        client=client,
        store=ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60),
        system_prompt="系统提示",
        max_input_chars=100,
        cooldown_seconds=0,
        max_concurrent_requests=2,
        model_router=model_router(),
        model_guard=guard,
        web_search_enabled=False,
    )
    try:
        await service.validate_queued("你好")
        guard.ensure_enabled.assert_awaited_once_with("chat-model", capability="agent")
        arguments = {"delivery": _deliver} if entry == "respond" else (
            {"request_id": "queued-1"} if entry == "generate_queued" else {}
        )
        await getattr(service, entry)("private:1", "你好", **arguments)
        assert client.calls[0]["use_agent"] is True
        assert client.calls[0]["use_web_search"] is False
        await service.ask_once("private:1", "总结消息", system_prompt="只总结原文")
        assert client.calls[1]["use_agent"] is False
        assert client.calls[1]["use_web_search"] is False
    finally:
        await service.aclose()
