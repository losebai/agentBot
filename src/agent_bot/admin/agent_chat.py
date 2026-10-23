"""管理 Agent 对话使用服务器授予的能力、上传附件和运行时租约。"""

from contextlib import aclosing

from pydantic import Field, model_validator

from agent_bot.admin.models import ApiModel
from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority
from agent_bot.agent.errors import AgentError
from agent_bot.models import ChatMessage
from agent_bot.multimodal import DEFAULT_IMAGE_PROMPT


class AgentProbeRequest(ApiModel):
    """不接受 QQ 地址或身份，只接受当前会话的上传 ID。"""

    agent_code: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    prompt: str = Field(max_length=2000)
    attachment_ids: tuple[str, ...] = Field(default=(), max_length=8)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=24)
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    request_id: str | None = Field(default=None, min_length=1, max_length=128)
    max_steps: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0, le=3600, allow_inf_nan=False)

    @model_validator(mode="after")
    def history(self):
        """纯图片可使用默认提示，历史禁止系统角色。"""
        if (
            (not self.prompt.strip() and not self.attachment_ids)
            or any(m.role == "system" for m in self.messages)
            or sum(len(m.content) for m in self.messages) > 12000
        ):
            raise ValueError("invalid agent history")
        return self


class AgentChatProbe:
    """管理入口的工具权限来自当前服务配置，客户端不能扩大。"""

    def __init__(self, runtime):
        """保留运行时提供者，不缓存已退役的服务实例。"""
        self.runtime = runtime

    def request(self, body, request_id):
        """会话与附件归属来自服务端；长视频预算只沿用管理员明确配置的超时。"""
        config = self.runtime.config
        timeout = min(config.chat_timeout_seconds, 3600)
        if body.timeout_seconds is not None:
            if body.timeout_seconds > timeout:
                raise AgentError("AGENT_TIMEOUT_INVALID", status_code=422)
            timeout = body.timeout_seconds
        return AgentRequest(
            agent_code=body.agent_code,
            prompt=body.prompt or DEFAULT_IMAGE_PROMPT,
            messages=(
                *body.messages,
                ChatMessage(role="user", content=body.prompt or DEFAULT_IMAGE_PROMPT),
            ),
            attachment_ids=body.attachment_ids,
            session_id=f"admin_{body.session_id}",
            request_id=body.request_id or request_id,
            max_steps=body.max_steps,
            timeout_seconds=timeout,
        )

    def service(self):
        """只在已持有运行时租约时调用。"""
        service = getattr(self.runtime, "agent_service", None)
        if service is None:
            raise AgentError("AGENT_NOT_INITIALIZED", status_code=503)
        return service

    def authority(self):
        """运行时真实可用的管理媒体工具才可加入授权。"""
        names = ("web_search", "read_url")
        if getattr(self.service().tool_runtime, "knowledge_store", None) is not None:
            names += ("knowledge_search",)
        if getattr(self.service().tool_runtime, "material_store", None) is not None:
            names += ("material_search", "material_get")
        adapter = getattr(self.service().tool_runtime, "admin_media_tools", None)
        if adapter is not None:
            names += tuple(sorted(adapter.allowed_tools))
        return ExecutionAuthority(entry="admin", allowed_tools=names)

    async def complete(self, body, request_id):
        """同步执行到完成或取消收尾后才归还配置租约。"""
        async with self.runtime.lease():
            request = self.request(body, request_id)
            return await self.service().complete(request, self.authority())

    async def stream(self, body, request_id):
        """流式和同步共用请求构造与管理工具授权。"""
        async with self.runtime.lease():
            request = self.request(body, request_id)
            async with aclosing(
                self.service().stream(
                    request,
                    self.authority(),
                )
            ) as events:
                async for event in events:
                    yield event
