"""聊天服务依赖的协议契约。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Protocol

from agent_bot.models import (
    ChatContext,
    ChatMediaResponse,
    ChatMessage,
    ChatToolName,
    LlmRequestOptions,
    MediaOutputType,
)


class ChatClient(Protocol):
    """dispatch-ai 客户端能力边界。"""

    def media_route(self, output_type: MediaOutputType):
        """路由客户端提供媒体能力预检决策；原 Java 适配器保持兼容。"""
        ...

    async def complete(
        self,
        *,
        model: str,
        session_id: str,
        prompt: str,
        messages: Sequence[ChatMessage],
        request_id: str,
        user: str | None = None,
        chat_context: ChatContext | None = None,
        image_urls: Sequence[str] = (),
        video_urls: Sequence[str] = (),
        use_agent: bool = True,
        use_web_search: bool = True,
        request_options: LlmRequestOptions | None = None,
        chat_tools: Sequence[ChatToolName] = (),
        tool_session_token: str | None = None,
    ) -> str:
        """生成一次文本回复。"""

        ...

    async def generate_media(
        self,
        *,
        model: str,
        session_id: str,
        prompt: str | None,
        messages: Sequence[ChatMessage],
        request_id: str,
        output_type: MediaOutputType,
        timeout_seconds: float,
        max_bytes: int,
        image_format: str | None,
        voice: str | None,
        audio_format: str | None,
        user: str,
    ) -> ChatMediaResponse:
        """生成一次图片或音频响应。"""

        ...


ReplyDelivery = Callable[[str], Awaitable[str]]
MediaReplyDelivery = Callable[[ChatMediaResponse], Awaitable[str]]
