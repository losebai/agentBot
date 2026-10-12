"""Ordinary AI text probes cannot execute robot tools or deliver QQ messages."""

from contextlib import aclosing

from agent_bot.admin.models import ChatRequest, ChatResponse
from agent_bot.ai.contracts import AiEvent, TextRequest
from agent_bot.ai.errors import AiError
from agent_bot.models import ChatMessage, LlmRequestOptions


class ChatProbe:
    """Use the live runtime under its lease without changing bot conversation state."""

    def __init__(self, runtime) -> None:
        """Hold the runtime access boundary rather than a stale client instance."""
        self.runtime = runtime

    async def complete(self, request: ChatRequest, request_id: str) -> ChatResponse:
        """Validate the configured model and send one ordinary completion to Java AI."""
        async with self.runtime.lease():
            config = self.runtime.config
            messages = [ChatMessage(role="system", content=config.dispatch_chat_system_prompt)]
            messages.extend(item for item in request.messages if item.role != "system")
            messages.append(ChatMessage(role="user", content=request.prompt))
            if config.dispatch_chat_plain_ai_backend == "python":
                result = await self.runtime.ai_service.complete(
                    self.text_request(request, request_id, messages)
                )
                return ChatResponse(
                    content=result.content, request_id=request_id, metadata=result.metadata()
                )
            if request.model is not None:
                raise AiError("AI_MODEL_SELECTION_REQUIRES_PYTHON", status_code=409)
            await self.runtime.model_guard.ensure_enabled(config.dispatch_chat_model)
            content = await self.runtime.client.complete(
                model=config.dispatch_chat_model,
                session_id=f"admin_{request.session_id}",
                prompt=request.prompt,
                messages=messages,
                request_id=request_id,
                use_agent=False,
                use_web_search=False,
                chat_tools=(),
            )
        return ChatResponse(content=content, request_id=request_id)

    def text_request(self, request: ChatRequest, request_id: str, messages) -> TextRequest:
        config = self.runtime.config
        options = LlmRequestOptions(
            **{
                name: getattr(config, f"dispatch_chat_llm_{name}")
                for name in LlmRequestOptions.model_fields
            }
        )
        return TextRequest(
            model=request.model or config.dispatch_chat_model,
            session_id=f"admin_{request.session_id}",
            request_id=request_id,
            prompt=request.prompt,
            messages=messages,
            options=options,
            timeout_seconds=min(config.dispatch_chat_timeout_seconds, 300),
        )

    async def stream(self, request: ChatRequest, request_id: str):
        async with self.runtime.lease():
            if self.runtime.config.dispatch_chat_plain_ai_backend != "python":
                yield AiEvent(
                    kind="error",
                    request_id=request_id,
                    code="AI_STREAM_REQUIRES_PYTHON",
                    message="普通对话后端已切换，请重新加载",
                )
                return
            messages = [
                ChatMessage(role="system", content=self.runtime.config.dispatch_chat_system_prompt),
                *(item for item in request.messages if item.role != "system"),
                ChatMessage(role="user", content=request.prompt),
            ]
            async with aclosing(
                self.runtime.ai_service.stream(self.text_request(request, request_id, messages))
            ) as events:
                async for event in events:
                    yield event
