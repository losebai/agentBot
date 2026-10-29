"""将共享表情库绑定到真实入口，原件只交付附件而不再次喂给聊天模型。"""

from __future__ import annotations

import base64
import json

from agent_bot.agent.contracts import ToolDefinition, ToolResult
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.local_media import project_local_schema, validate_local_call
from agent_bot.media_artifacts import ImageArtifact
from agent_bot.plugins.stickers.tool_contracts import (
    STICKER_TOOLS,
    StickerSearchArguments,
    StickerSendArguments,
)
from agent_bot.services.tool_images import record_tool_images

_DESCRIPTIONS = {
    "sticker_search": (
        "根据当前聊天语境、情绪和表达意图检索表情包。context写清楚本轮对话含义和语气，"
        "可传tags筛选管理员标签。日常闲聊可自主检索并搭配表情，无需用户明确要求发图。"
        "检索只返回已入库的真实ID、说明、情绪、适用场景及相关度。"
        "没有合适表情返回空列表，不强行发送；自然语言回复不展示检索评分和内部ID。"
    ),
    "sticker_send": (
        "选择本请求sticker_search推荐的一张表情包，随本轮回复投递，保留原始动画。"
        "可以按聊天语气自主选图，无需用户确认；当前请求至多一张，可搭配简短自然文字。"
        "id必须来自本轮检索，不能凭空编造或传URL、路径。"
        "成功只表示已登记到本轮投递，不能声称独立消息已送达，也不需要等待或轮询。"
    ),
}


class StickerTools:
    """插件实时服务决定可用性，保存的Agent工具配置不能复活停用插件。"""

    def __init__(self, service_provider) -> None:
        """服务由宿主延迟提供，配置热刷新仍读取当前唯一图库。"""
        self.service_provider = service_provider

    @property
    def allowed_tools(self) -> frozenset[str]:
        """未启动插件时不向任何入口授予表情工具。"""
        return STICKER_TOOLS if self.service_provider() is not None else frozenset()

    def bind(self, request, authority, scope, media_store, bridge):
        """请求级选择状态不在不同会话或不同Agent执行间共享。"""
        return StickerToolSession(self, request, authority, scope, media_store, bridge)


class StickerToolSession:
    """搜索与发送共享本轮真实候选集合，并限制只能登记一张表情。"""

    def __init__(self, tools, request, authority, scope, media_store, bridge) -> None:
        """作用域、工具白名单和会话令牌均由入口提供，模型不能修改。"""
        self.tools, self.request, self.authority = tools, request, authority
        self.scope, self.media_store, self.bridge = scope, media_store, bridge
        self.selected: set[str] = set()
        self.sent_id: str | None = None
        models = {"sticker_search": StickerSearchArguments, "sticker_send": StickerSendArguments}
        self.definitions = tuple(ToolDefinition(
            name=name, source="builtin", description=_DESCRIPTIONS[name],
            input_schema=project_local_schema(name, model.model_json_schema()),
        ) for name, model in models.items() if name in tools.allowed_tools)

    async def _authorize(self, name: str):
        """每次调用复核令牌、原始会话与白名单，不能借另一会话的图片权限。"""
        if name not in self.authority.allowed_tools or name not in STICKER_TOOLS:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        if self.authority.entry == "qq":
            if self.bridge is None:
                raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
            context = await self.bridge.context(self.authority)
            context.ensure_active()
            if context.session_id != self.request.session_id or name not in context.allowed_tools:
                raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        service = self.tools.service_provider()
        if service is None:
            raise AgentError("STICKER_DISABLED", status_code=503)
        return service

    async def call(self, call) -> ToolResult:
        """文字结果只含元数据，投递原件独立进入现有持久附件链。"""
        service = await self._authorize(call.name)
        validate_local_call(call)
        if call.name == "sticker_search":
            args = StickerSearchArguments.model_validate(call.arguments)
            value = await service.search(args.context, tags=args.tags, limit=args.limit)
            self.selected.update(match["sticker"]["id"] for match in value["matches"])
            return ToolResult(call_id=call.call_id, name=call.name,
                              content=json.dumps(value, ensure_ascii=False))
        args = StickerSendArguments.model_validate(call.arguments)
        if args.id not in self.selected:
            raise AgentError("STICKER_NOT_SELECTED", status_code=403)
        if self.sent_id is not None:
            if self.sent_id != args.id:
                raise AgentError("STICKER_SEND_LIMIT", status_code=409)
            return ToolResult(call_id=call.call_id, name=call.name,
                              content='{"status":"already_registered"}')
        item, data = await service.store.read(args.id)
        if not item.enabled:
            raise AgentError("STICKER_UNAVAILABLE", status_code=409)
        await self._authorize(call.name)
        attachments = ()
        if self.authority.entry == "qq":
            record_tool_images((ImageArtifact(
                mime_type=item.mime_type, data=base64.b64encode(data).decode("ascii"),
            ),))
        else:
            if self.media_store is None or self.scope is None:
                raise AgentError("MEDIA_NOT_READY", status_code=503)
            attachments = (await self.media_store.put(
                owner=self.scope.owner, scope=self.scope, purpose="output",
                data=data, mime_type=item.mime_type,
            ),)
        self.sent_id = args.id
        return ToolResult(
            call_id=call.call_id, name=call.name,
            content=json.dumps({"id": item.id, "name": item.name,
                                "status": "registered_for_delivery"}, ensure_ascii=False),
            attachments=attachments,
        )
