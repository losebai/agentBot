"""按入口绑定已授权工具，持有远端资源并在副作用前完整验证参数。"""

from contextlib import asynccontextmanager

from agent_bot.agent.errors import AgentError
from agent_bot.agent.tools.knowledge import KnowledgeSearchTool
from agent_bot.agent.tools.local_media import validate_local_call
from agent_bot.agent.tools.materials import MaterialGetTool, MaterialSearchTool
from agent_bot.agent.tools.policy import ADMIN_MEDIA_TOOLS, CHAT_TOOLS, ToolPolicy
from agent_bot.agent.tools.public_http import PublicPageReader
from agent_bot.agent.tools.read_url import ReadUrlTool
from agent_bot.agent.tools.schema import ToolSchema
from agent_bot.agent.tools.search import SearchTool
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
        if definition.source == "builtin" and definition.name == "web_search":
            SearchTool.normalize_query(call.arguments["query"])
        if definition.source == "builtin" and definition.name == "knowledge_search":
            KnowledgeSearchTool.validate(call.arguments)
        if definition.source == "builtin" and definition.name in {
            "material_search", "material_get",
        }:
            validate_local_call(call)
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
                 job_tools=None, knowledge_store=None, material_store=None, media_store=None):
        """本地适配器引用当前运行时的客户端与任务存储。"""
        self.connector, self.chat_bridge = connector, chat_bridge
        self.reader = reader or PublicPageReader()
        self.admin_media_tools = admin_media_tools
        self.job_tools = job_tools
        self.knowledge_store = knowledge_store
        self.material_store = material_store
        self.media_store = media_store

    @asynccontextmanager
    async def open(self, snapshot, request, authority, *, media_scope=None, images=()):
        """关联 MCP 自动开放白名单内工具，内置工具仍遵循预设与入口授权交集。"""
        if request.tools == ():
            yield BoundTools((), {})
            return
        servers = snapshot.mcps
        if any(s.category != "general" for s in servers):
            raise AgentError("AGENT_MCP_CATEGORY_INVALID", status_code=422)
        if not {s.code for s in servers} <= set(authority.allowed_mcp_services):
            raise AgentError("AGENT_TOOL_UNAUTHORIZED", status_code=403)
        async with self.connector.open(servers, remaining=lambda: remaining_timeout(300)) as remote:
            definitions, handlers = list(remote.tools), {t.name: remote.call for t in remote.tools}
            auth = authority.model_copy(
                update={
                    "allowed_tools": tuple(
                        dict.fromkeys((*authority.allowed_tools, *(t.name for t in remote.tools)))
                    )
                }
            )
            builtin_tools = [
                ReadUrlTool(self.reader), SearchTool(self.connector, snapshot.search_mcp)
            ]
            if self.knowledge_store is not None:
                builtin_tools.append(KnowledgeSearchTool(self.knowledge_store))
            if self.material_store is not None and media_scope is not None:
                builtin_tools.append(MaterialSearchTool(self.material_store, media_scope.owner))
                builtin_tools.append(MaterialGetTool(
                    self.material_store, media_scope, self.media_store,
                ))
            for tool in builtin_tools:
                definitions.append(tool.definition)
                if tool.definition.name in handlers:
                    raise AgentError("AGENT_TOOL_NAME_CONFLICT", status_code=409)
                handlers[tool.definition.name] = tool.call
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
            if any(t.name == "web_search" for t in selected) and snapshot.search_mcp is None:
                raise AgentError("AGENT_SEARCH_UNAVAILABLE", status_code=409)
            yield BoundTools(selected, handlers, remote.warnings)
