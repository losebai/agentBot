from __future__ import annotations

from collections.abc import Sequence

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
@pytest.mark.parametrize("agent_enabled", [True, False])
async def test_service_propagates_remote_tool_context_without_persisting_the_token(
    web_search_enabled: bool, agent_enabled: bool,
) -> None:
    """本地工具与联网能力可同时提供，联网能力仍受两个配置开关控制。"""

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
        agent_enabled=agent_enabled,
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
    assert client.calls[0]["use_web_search"] is (agent_enabled and web_search_enabled)
    persisted = store.context("group:1")
    assert token not in "".join(message.content for message in persisted.messages)


@pytest.mark.asyncio
async def test_service_uses_agent_for_general_mcp_when_web_search_is_disabled() -> None:
    """配置了通用 MCP 时，关闭联网搜索也不能退回无工具聊天。"""

    client = CapturingClient()
    service = ChatService(
        client=client,
        store=ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60),
        system_prompt="系统提示",
        max_input_chars=100,
        cooldown_seconds=0,
        max_concurrent_requests=2,
        model_router=model_router(),
        agent_enabled=True,
        web_search_enabled=False,
        mcp_services_enabled=True,
    )

    await service.ask("private:1", "查看当前窗口")

    assert client.calls[0]["use_agent"] is True
    assert client.calls[0]["use_web_search"] is False


async def _deliver(answer: str) -> str:
    return answer
