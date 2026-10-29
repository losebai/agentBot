"""按入口绑定已授权工具，持有远端资源并在副作用前完整验证参数。"""

from contextlib import asynccontextmanager

from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.host_contracts import HOST_TOOLS
from agent_bot.agent.tools.knowledge import KnowledgeSearchTool
from agent_bot.agent.tools.local_media import validate_local_call
from agent_bot.agent.tools.materials import MaterialGetTool, MaterialSearchTool
from agent_bot.agent.tools.policy import ADMIN_MEDIA_TOOLS, CHAT_TOOLS, ToolPolicy
from agent_bot.agent.tools.public_http import PublicPageReader
from agent_bot.agent.tools.read_url import ReadUrlTool
from agent_bot.agent.tools.schema import ToolSchema
from agent_bot.services.request_deadline import remaining_timeout


class BoundTools:
    """当前执行已选中的定义与处理器，调用方不能绕过预检。"""

    def __init__(self, definitions, handlers, warnings=()):
        """定义与处理器来自同一运行时快照。"""
        self.definitions, self.handlers, self.warnings = definitions, handlers, warnings

    def validate(self, call):
        """同步验证授权、通用 schema 和本地原参数，不触发网络或工具。"""
        definition = next((item for item in self.definitions if item.name == call.name), None)
        if definition is None or call.name not in self.handlers:
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        ToolSchema.validate_arguments(definition, call.arguments)
        if definition.source == 'builtin':
            # 登记过的本地模型保留格式和跨字段约束，整批副作用前先验证。
            validate_local_call(call)
        if definition.source == "builtin" and definition.name == "knowledge_search":
            KnowledgeSearchTool.validate(call.arguments)
        if definition.source in {"chat", "media"}:
            validate_local_call(call)

    async def call(self, call):
        """单独调用也必须验证，整批执行会预先验证全部项目。"""
        self.validate(call)
        handler = self.handlers.get(call.name)
        if handler is None or not any(t.name == call.name for t in self.definitions):
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        return await handler(call)


class ToolRuntime:
    """QQ 和管理入口使用各自媒体适配器，禁止混用身份。"""

    def __init__(self, connector, chat_bridge=None, reader=None, *, admin_media_tools=None,
                 job_tools=None, knowledge_store=None, material_store=None, media_store=None,
                 bilibili_tools=None, host_tools=None):
        """本地适配器引用当前运行时的客户端与任务存储。"""
        self.connector, self.chat_bridge = connector, chat_bridge
        self.reader = reader or PublicPageReader()
        self.admin_media_tools = admin_media_tools
        self.job_tools = job_tools
        self.knowledge_store = knowledge_store
        self.material_store = material_store
        self.media_store = media_store
        self.bilibili_tools = bilibili_tools
        self.host_tools = host_tools

    @asynccontextmanager
    async def open(self, snapshot, request, authority, *, media_scope=None, images=()):
        """工具遵循预设与入口授权交集，主人素材权限由真实令牌逐次验证。"""
        if request.tools == ():
            yield BoundTools((), {})
            return
        servers = snapshot.mcps
        if not {s.code for s in servers} <= set(authority.allowed_mcp_services):
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        async with self.connector.open(
            servers, remaining=lambda: remaining_timeout(300),
            request_id=media_scope.execution_id if media_scope is not None else None,
        ) as remote:
            definitions, handlers = list(remote.tools), {t.name: remote.call for t in remote.tools}
            auth = authority.model_copy(
                update={
                    "allowed_tools": tuple(
                        dict.fromkeys((*authority.allowed_tools, *(t.name for t in remote.tools)))
                    )
                }
            )
            builtin_tools = [ReadUrlTool(self.reader)]
            if self.knowledge_store is not None:
                builtin_tools.append(KnowledgeSearchTool(self.knowledge_store))
            if self.material_store is not None and media_scope is not None:

                async def material_library_access() -> bool:
                    """只信任当前执行的 QQ 令牌，不接受模型指定身份或借用其他会话。"""
                    if (authority.entry != "qq" or media_scope.owner.entry != "qq"
                            or self.chat_bridge is None or authority.chat_token is None):
                        return False
                    context = await self.chat_bridge.context(authority)
                    context.ensure_active()
                    if context.session_id != media_scope.owner.session_id:
                        raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
                    return context.material_library_access

                builtin_tools.append(MaterialSearchTool(
                    self.material_store, media_scope.owner, library_access=material_library_access,
                ))
                builtin_tools.append(MaterialGetTool(
                    self.material_store, media_scope, self.media_store,
                    library_access=material_library_access,
                ))
            for tool in builtin_tools:
                definitions.append(tool.definition)
                if tool.definition.name in handlers:
                    raise AgentError("AGENT_TOOL_NAME_CONFLICT", status_code=409)
                handlers[tool.definition.name] = tool.call
            if self.bilibili_tools is not None:
                session = self.bilibili_tools.bind(
                    request, authority, media_scope, self.media_store, self.chat_bridge,
                )
                for definition in session.definitions:
                    if definition.name in handlers:
                        raise AgentError('AGENT_TOOL_NAME_CONFLICT', status_code=409)
                    definitions.append(definition)
                    handlers[definition.name] = session.call
            if self.host_tools is not None:
                # 宿主工具单独绑定真实入口身份，仍由后续策略取 Agent 与入口授权交集。
                session = self.host_tools.bind(request, authority, self.chat_bridge)
                for definition in session.definitions:
                    if definition.name in handlers:
                        raise AgentError('AGENT_TOOL_NAME_CONFLICT', status_code=409)
                    definitions.append(definition)
                    handlers[definition.name] = session.call
            if authority.entry == "qq" and set(authority.allowed_tools) & CHAT_TOOLS:
                if self.chat_bridge is None:
                    raise AgentError("AGENT_CHAT_TOOLS_UNAVAILABLE", status_code=503)
                local = await self.chat_bridge.list_tools(authority)
                definitions.extend(local)

                async def invoke_chat(call):
                    """每次 QQ 调用重新验证会话令牌。"""
                    return await self.chat_bridge.call(call, authority)

                for tool in local:
                    if tool.name in handlers:
                        raise AgentError("AGENT_TOOL_NAME_CONFLICT", status_code=409)
                    handlers[tool.name] = invoke_chat
            if authority.entry == "qq" and "job" in authority.allowed_tools:
                if self.job_tools is None or self.chat_bridge is None:
                    raise AgentError("JOB_NOT_INITIALIZED", status_code=503)
                session = self.job_tools.bind(
                    snapshot, request, await self.chat_bridge.context(authority)
                )

                async def invoke_job(call):
                    """每次执行重新解析令牌并绑定身份，过期令牌不能沿用旧授权。"""
                    current = self.job_tools.bind(
                        snapshot, request, await self.chat_bridge.context(authority)
                    )
                    return await current.call(call)

                if "job" in handlers:
                    raise AgentError("AGENT_TOOL_NAME_CONFLICT", status_code=409)
                definitions.extend(session.definitions)
                handlers["job"] = invoke_job
            if authority.entry == "admin" and set(authority.allowed_tools) & ADMIN_MEDIA_TOOLS:
                if self.admin_media_tools is None or media_scope is None:
                    raise AgentError("MEDIA_NOT_READY", status_code=503)
                session = self.admin_media_tools.bind(media_scope, images)
                definitions.extend(session.definitions)
                for tool in session.definitions:
                    if tool.name in handlers:
                        raise AgentError("AGENT_TOOL_NAME_CONFLICT", status_code=409)
                    handlers[tool.name] = session.call
            # 服务授权与白名单已在连接和发现阶段校验，无需再逐个写入 Agent 工具配置。
            configured = (
                tuple(dict.fromkeys((*snapshot.agent.tools, *(t.name for t in remote.tools))))
                if snapshot.agent else auth.allowed_tools
            )
            if snapshot.agent is None and self.host_tools is not None:
                # 主人入口有资格不等于默认授权本机操作；必须在命名 Agent 中显式勾选。
                configured = tuple(name for name in configured if name not in HOST_TOOLS)
            if remote.warnings and request.tools is None:
                unavailable = {w.service_code for w in remote.warnings}
                available = {t.name for t in definitions}
                omitted = {
                    name for s in servers if s.code in unavailable for name in s.allowed_tools
                }
                configured = tuple(
                    name
                    for name in configured
                    if name in available or (name not in omitted and "*" not in omitted)
                )
            selected = ToolPolicy.select(
                configured=configured,
                requested=request.tools,
                authority=auth,
                available=tuple(definitions),
            )
            yield BoundTools(selected, handlers, remote.warnings)
