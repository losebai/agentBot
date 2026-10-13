"""管理文本测试直接使用本地 AI 服务，不执行机器人工具或投递 QQ。"""

from contextlib import aclosing

from agent_bot.admin.models import ChatRequest, ChatResponse
from agent_bot.ai.contracts import TextRequest
from agent_bot.models import ChatMessage, LlmRequestOptions


class ChatProbe:
    """在运行时租约中执行独立管理会话，不修改机器人对话状态。"""

    def __init__(self, runtime) -> None:
        """持有运行时访问边界，避免热更新后继续使用旧客户端。"""
        self.runtime = runtime

    async def complete(self, request: ChatRequest, request_id: str) -> ChatResponse:
        """构造受信任系统提示，通过 Python 模型目录完成一次普通对话。"""
        async with self.runtime.lease():
            config = self.runtime.config
            messages = [ChatMessage(role="system", content=config.chat_system_prompt)]
            messages.extend(item for item in request.messages if item.role != "system")
            messages.append(ChatMessage(role="user", content=request.prompt))
            result = await self.runtime.ai_service.complete(
                self.text_request(request, request_id, messages)
            )
            return ChatResponse(
                content=result.content, request_id=request_id, metadata=result.metadata()
            )

    def text_request(self, request: ChatRequest, request_id: str, messages) -> TextRequest:
        """只允许模型别名和有界历史，由服务端配置生成协议参数。"""
        config = self.runtime.config
        options = LlmRequestOptions(
            **{
                name: getattr(config, f"chat_llm_{name}")
                for name in LlmRequestOptions.model_fields
            }
        )
        return TextRequest(
            model=request.model or config.chat_model,
            session_id=f"admin_{request.session_id}",
            request_id=request_id,
            prompt=request.prompt,
            messages=messages,
            options=options,
            timeout_seconds=min(config.chat_timeout_seconds, 300),
        )

    async def stream(self, request: ChatRequest, request_id: str):
        """整个流式响应持有同一运行时，断开时关闭模型输出迭代器。"""
        async with self.runtime.lease():
            messages = [
                ChatMessage(role="system", content=self.runtime.config.chat_system_prompt),
                *(item for item in request.messages if item.role != "system"),
                ChatMessage(role="user", content=request.prompt),
            ]
            async with aclosing(
                self.runtime.ai_service.stream(self.text_request(request, request_id, messages))
            ) as events:
                async for event in events:
                    yield event
