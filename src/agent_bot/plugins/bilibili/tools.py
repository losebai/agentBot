"""Agent B 站工具绑定当前入口与真实 QQ 身份，不接受模型自报身份。"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_bot.agent.contracts import (
    AgentRequest,
    ExecutionAuthority,
    ToolCall,
    ToolDefinition,
    ToolResult,
)
from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.local_media import project_local_schema, validate_local_call
from agent_bot.media.contracts import MediaOwner, MediaScope
from agent_bot.plugins.bilibili.contracts import BILIBILI_TOOLS, TOOL_MODELS, WRITE_TOOLS

if TYPE_CHECKING:
    from agent_bot.agent.tools.chat import ChatToolBridge
    from agent_bot.media.store import MediaStore
    from agent_bot.plugins.bilibili.service import BilibiliService

DESCRIPTIONS = {
    'bilibili_login_status': '实际检测 B 站当前账号登录状态；不返回 Cookie。',
    'bilibili_get_my_videos': '查看我的B站视频列表、我的投稿：分页读取当前登录账号已发布的视频，'
                              '返回 BV/AV 标识、标题、发布时间和状态；不是收藏或浏览历史。'
                              '可先列出视频，再调用 bilibili_get_comments 读取所选视频评论。',
    'bilibili_get_comments': '按视频 BV/AV 号读取一页评论；root 用于读取楼中楼。'
                              '没有后台监听；评论过长时可减小 page_size。',
    'bilibili_reply_comment': '以当前 B 站账号回复指定视频评论；root 是根楼层，parent 是目标评论。'
                              '只有管理员或 QQ 超级用户可用；请先读取评论确认 ID。',
    'bilibili_publish_video': '投稿当前会话的 MP4 视频和封面图片资产，必须提供 asset_id，'
                              '不可传文件路径或 URL。返回已提交审核，不表示已公开。'
                              '转载 copyright=2 时必须提供 source。',
    'bilibili_publish_dynamic': '发布B站图文动态：提供正文 text 和按顺序排列的 image_asset_ids。'
                                '只接受当前会话的 PNG/JPEG 图片资产，1到9张、正文最多2000字；'
                                '可使用本次上传或工具生成的图片ID，不接受路径或远程URL。'
                                '仅管理员或QQ超级用户可用，返回已提交不保证立即公开。',
}


class BilibiliTools:
    """提供者跨公共配置热刷新复用，不缓存旧客户端与超级用户列表。"""

    def __init__(self, service_provider: Callable[[], BilibiliService | None],
                 superusers_provider: Callable[[], set[str]]) -> None:
        """保存宿主提供者，禁用插件时没有工具能力。"""
        self.service_provider = service_provider
        self.superusers_provider = superusers_provider

    @property
    def allowed_tools(self) -> frozenset[str]:
        """仅运行中插件可以出现在管理入口的服务端授权中。"""
        return BILIBILI_TOOLS if self.service_provider() is not None else frozenset()

    def bind(self, request: AgentRequest, authority: ExecutionAuthority,
             media_scope: MediaScope | None, media_store: MediaStore | None,
             chat_bridge: ChatToolBridge | None) -> BilibiliToolSession:
        """每个调用会重新校验身份，绑定时不能永久授予写权限。"""
        return BilibiliToolSession(self, request, authority, media_scope, media_store, chat_bridge)


class BilibiliToolSession:
    """schema 预检与实际执行共用同一严格参数模型。"""

    def __init__(self, tools: BilibiliTools, request: AgentRequest,
                 authority: ExecutionAuthority, scope: MediaScope | None,
                 media_store: MediaStore | None, bridge: ChatToolBridge | None) -> None:
        """定义仍须由 ToolPolicy 与入口和 Agent 白名单求交集。"""
        self.tools, self.request, self.authority = tools, request, authority
        self.owner = scope.owner if scope else MediaOwner(
            entry=authority.entry, session_id=request.session_id)
        self.media_store, self.bridge = media_store, bridge
        self.definitions = tuple(ToolDefinition(
            name=name, source='builtin', description=DESCRIPTIONS[name] + (
                '\n本次上传的图片资产 ID：' + ', '.join(request.attachment_ids)
                if name in {'bilibili_publish_video', 'bilibili_publish_dynamic'}
                and request.attachment_ids else ''),
            input_schema=project_local_schema(name, model.model_json_schema()),
        ) for name, model in TOOL_MODELS.items() if name in tools.allowed_tools)

    async def call(self, call: ToolCall) -> ToolResult:
        """QQ 令牌和真实发送者每次验证，配置工具本身不能授予账号写权限。"""
        if (call.name not in self.authority.allowed_tools
                or call.name not in self.tools.allowed_tools):
            raise AgentError('AGENT_TOOL_UNAUTHORIZED', status_code=403)
        validate_local_call(call)
        args = TOOL_MODELS[call.name].model_validate(call.arguments)
        if self.authority.entry == 'qq':
            if self.bridge is None:
                raise AgentError('AGENT_TOOL_UNAUTHORIZED', status_code=403)
            context = await self.bridge.context(self.authority)
            context.ensure_active()
            if (call.name not in context.allowed_tools or (
                call.name in WRITE_TOOLS
                and context.user_id not in self.tools.superusers_provider()
            )):
                raise AgentError('AGENT_TOOL_UNAUTHORIZED', status_code=403)
        service = self.tools.service_provider()
        if service is None:
            raise AgentError('BILIBILI_DISABLED', status_code=503)
        if call.name == 'bilibili_login_status':
            value = await service.status()
        elif call.name == 'bilibili_get_my_videos':
            value = await service.my_videos(args)
        elif call.name == 'bilibili_get_comments':
            value = await service.comments(args)
        else:
            value = await service.write(call.name, args, owner=self.owner,
                                        request_id=self.request.request_id,
                                        media_store=self.media_store)
        content = json.dumps(value, ensure_ascii=False)
        if len(content) > 32000:
            raise AgentError('AGENT_TOOL_RESULT_TOO_LARGE', status_code=422)
        return ToolResult(call_id=call.call_id, name=call.name, content=content)
