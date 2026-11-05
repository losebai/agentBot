"""已认证的 Agent 目录、执行和记录管理接口。"""

from contextlib import aclosing
from typing import Literal

from fastapi import APIRouter, Query, Request
from starlette.responses import Response

from agent_bot.admin.agent_chat import AgentChatProbe, AgentProbeRequest
from agent_bot.admin.mcp_tools import list_server_tools
from agent_bot.admin.media_requests import run_with_disconnect
from agent_bot.admin.streaming import ManagedStreamingResponse
from agent_bot.agent.contracts import (
    AgentCompletion,
    AgentCreate,
    AgentUpdate,
    AgentView,
    McpCreate,
    McpUpdate,
    McpView,
    RunDetail,
    RunSummary,
    SkillCreate,
    SkillUpdate,
    SkillView,
    StepView,
    ToolDefinition,
)
from agent_bot.agent.errors import AgentError
from agent_bot.ai.contracts import CatalogPage, VersionDelete
from agent_bot.evolution.contracts import EvolutionAdvance, EvolutionConfigUpdate, EvolutionReset


def install_agent_routes(router: APIRouter, runtime):
    """安装 Agent 管理接口，执行时从当前租约获取服务和授权。"""
    chat = AgentChatProbe(runtime)

    async def evolution_operation(action, *args, **kwargs):
        """演化管理沿用外层管理员认证，变更必须持有正常运行时租约。"""
        async with runtime.lease(read_only=action in {'profiles', 'history', 'records',
                                                     'record_history', 'preview'}):
            evolution = chat.service().evolution
            if evolution is None:
                raise AgentError('AGENT_NOT_INITIALIZED', status_code=503)
            return await getattr(evolution.store, action)(*args, **kwargs)

    @router.get('/evolution')
    async def independent_evolution_profiles(
        agent_id: int | None = Query(None, ge=1), cursor: int = Query(0, ge=0),
        page: int | None = Query(None, ge=1), limit: int = Query(10, ge=1, le=100),
    ):
        """默认查询无需角色的通用范围，也可显式选择关联角色。"""
        return await evolution_operation('profiles', agent_id, after=cursor, page=page, limit=limit)

    @router.patch('/evolution')
    async def independent_evolution_config(
        body: EvolutionConfigUpdate, agent_id: int | None = Query(None, ge=1),
    ):
        """通用范围和可选关联范围均按所见配置版本保存。"""
        return await evolution_operation('configure', agent_id, **body.model_dump())

    @router.get('/evolution/records')
    async def evolution_records(
        cursor: int = Query(0, ge=0),
        page: int | None = Query(None, ge=1), limit: int = Query(10, ge=1, le=100),
        kind: Literal['feedback', 'change', 'reset'] | None = None,
        agent_id: int | None = Query(None, ge=1), unbound: bool = False,
    ):
        """独立分页展示跨范围记录；筛选可选角色或所有未关联范围。"""
        return await evolution_operation('records', before=cursor, page=page, limit=limit,
                                         kind=kind, agent_id=agent_id, unbound=unbound)

    @router.get('/evolution/profiles/{profile_id}')
    async def evolution_record_history(
        profile_id: int, cursor: int = Query(0, ge=0),
        page: int | None = Query(None, ge=1), limit: int = Query(10, ge=1, le=100),
    ):
        """列表直接定位画像依据，角色已删除也不会使历史失联。"""
        return await evolution_operation(
            'record_history', profile_id, before=cursor, page=page, limit=limit
        )

    @router.post('/evolution/profiles/{profile_id}/reset')
    async def independent_evolution_reset(profile_id: int, body: EvolutionReset):
        """重置独立画像，角色关联不是管理前提，仍校验当前版本。"""
        return await evolution_operation('reset_profile', profile_id, **body.model_dump())

    @router.get('/evolution/profiles/{profile_id}/preview')
    async def evolution_preview(profile_id: int):
        """只读研究成熟度、预计档位与原始证据，不触发应用。"""
        return await evolution_operation('preview', profile_id)

    @router.post('/evolution/profiles/{profile_id}/advance')
    async def evolution_advance(profile_id: int, body: EvolutionAdvance):
        """按所见研究摘要手动执行一次演进，既有阈值和暂停保持有效。"""
        return await evolution_operation('advance_profile', profile_id, **body.model_dump())

    @router.get('/agents/{id}/evolution')
    async def evolution_profiles(
        id: int, cursor: int = Query(0, ge=0), page: int | None = Query(None, ge=1),
        limit: int = Query(10, ge=1, le=100),
    ):
        """分页查看匿名互动对象及当前演化状态。"""
        return await evolution_operation('profiles', id, after=cursor, page=page, limit=limit)

    @router.patch('/agents/{id}/evolution')
    async def evolution_config(id: int, body: EvolutionConfigUpdate):
        """暂停停止新反馈收集和习惯应用，不修改原角色。"""
        return await evolution_operation('configure', id, **body.model_dump())

    @router.get('/agents/{id}/evolution/profiles/{profile_id}')
    async def evolution_history(
        id: int, profile_id: int, cursor: int = Query(0, ge=0),
        page: int | None = Query(None, ge=1), limit: int = Query(10, ge=1, le=100),
    ):
        """历史包含原话和变化引用，分页不会跨 Agent 查询。"""
        return await evolution_operation(
            'history', id, profile_id, before=cursor, page=page, limit=limit
        )

    @router.post('/agents/{id}/evolution/profiles/{profile_id}/reset')
    async def evolution_reset(id: int, profile_id: int, body: EvolutionReset):
        """显式重置当前版本，旧证据不再参与演化但保留审计。"""
        return await evolution_operation('reset', id, profile_id, **body.model_dump())

    async def operate(action, *args, records=False, **kwargs):
        """仅数据库目录和归档可在刷新排空时读取，发现工具及执行仍需正常租约。"""
        read_only = action in {
            "list_agents", "get_agent", "portrait_content", "list_skills", "get_skill",
            "list_mcps", "get_mcp",
            "list_runs", "get_run", "list_steps",
        }
        async with runtime.lease(read_only=read_only):
            service = chat.service()
            result = await getattr(service.records if records else service.catalog, action)(
                *args, **kwargs
            )

            return result

    @router.get("/agents", response_model=CatalogPage[AgentView])
    async def list_agents(
        limit: int = Query(10, ge=1, le=100), cursor: str | None = Query(None, max_length=512),
        page: int | None = Query(None, ge=1),
    ):
        """角色页面优先显示运行配置实际选中的启用角色，其余顺序及分页保持稳定。"""
        return await operate("list_agents", limit=limit, cursor=cursor, page=page,
                             preferred_code=runtime.config.chat_python_agent_code)

    @router.get("/agents/{id}", response_model=AgentView)
    async def get_agent(id: int):
        return await operate("get_agent", id)

    @router.get("/agents/{id}/portrait")
    async def agent_portrait(id: int):
        """图片继承管理认证，禁用缓存避免登录切换后复用私有内容。"""
        row = await operate("portrait_content", id)
        return Response(bytes(row["image_data"]), media_type=row["mime_type"], headers={
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
        })

    @router.post("/agents", response_model=AgentView, status_code=201)
    async def create_agent(body: AgentCreate):
        return await operate("create_agent", body)

    @router.patch("/agents/{id}", response_model=AgentView)
    async def update_agent(id: int, body: AgentUpdate):
        return await operate("update_agent", id, body)

    @router.delete("/agents/{id}", status_code=204)
    async def delete_agent(id: int, body: VersionDelete):
        await operate("delete_agent", id, body.expected_revision)

    @router.get("/skills", response_model=CatalogPage[SkillView])
    async def list_skills(
        limit: int = Query(10, ge=1, le=100), cursor: str | None = Query(None, max_length=512),
        page: int | None = Query(None, ge=1),
    ):
        return await operate("list_skills", limit=limit, cursor=cursor, page=page)

    @router.get("/skills/{id}", response_model=SkillView)
    async def get_skill(id: int):
        return await operate("get_skill", id)

    @router.post("/skills", response_model=SkillView, status_code=201)
    async def create_skill(body: SkillCreate):
        return await operate("create_skill", body)

    @router.patch("/skills/{id}", response_model=SkillView)
    async def update_skill(id: int, body: SkillUpdate):
        return await operate("update_skill", id, body)

    @router.delete("/skills/{id}", status_code=204)
    async def delete_skill(id: int, body: VersionDelete):
        await operate("delete_skill", id, body.expected_revision)

    @router.get("/mcp-servers", response_model=CatalogPage[McpView])
    async def list_mcps(
        limit: int = Query(10, ge=1, le=100), cursor: str | None = Query(None, max_length=512),
        page: int | None = Query(None, ge=1),
    ):
        return await operate("list_mcps", limit=limit, cursor=cursor, page=page)

    @router.get("/mcp-servers/{id}", response_model=McpView)
    async def get_mcp(id: int):
        return await operate("get_mcp", id)

    @router.post("/mcp-servers", response_model=McpView, status_code=201)
    async def create_mcp(body: McpCreate):
        return await operate("create_mcp", body)

    @router.get("/mcp-servers/{id}/tools", response_model=tuple[ToolDefinition, ...])
    async def mcp_tools(id: int, request: Request):
        """只使用已认证目录快照；关闭弹窗断开连接时取消发现并归还运行时租约。"""
        async def discover():
            """整个 MCP 连接生命周期持有同一运行时租约。"""
            async with runtime.lease():
                server = await chat.service().catalog.get_mcp(id)
                return await list_server_tools(server)

        return await run_with_disconnect(request, discover())

    @router.patch("/mcp-servers/{id}", response_model=McpView)
    async def update_mcp(id: int, body: McpUpdate):
        return await operate("update_mcp", id, body)

    @router.delete("/mcp-servers/{id}", status_code=204)
    async def delete_mcp(id: int, body: VersionDelete):
        await operate("delete_mcp", id, body.expected_revision)

    @router.post("/agent-runs", response_model=AgentCompletion)
    async def complete(body: AgentProbeRequest, request: Request):
        """同步 Agent 同样监听连接断开，等待工具和文件收尾后退出。"""
        return await run_with_disconnect(request, chat.complete(body, request.state.request_id))

    @router.post("/agent-runs/stream")
    async def stream(body: AgentProbeRequest, request: Request):
        async with runtime.lease():
            chat.request(body, request.state.request_id)
            readiness = await chat.service().readiness()
            if not readiness.initialized:
                raise AgentError(readiness.code or "AGENT_NOT_INITIALIZED", status_code=503)

        async def content():
            async with aclosing(chat.stream(body, request.state.request_id)) as events:
                async for event in events:
                    data = event.model_dump_json(exclude_none=True)
                    yield f"event: {event.kind}\ndata: {data}\n\n"

        return ManagedStreamingResponse(
            content(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @router.get("/agent-runs", response_model=CatalogPage[RunSummary])
    async def runs(
        limit: int = Query(10, ge=1, le=100),
        cursor: str | None = Query(None, max_length=1024),
        page: int | None = Query(None, ge=1),
        session_id: str | None = Query(None, max_length=256),
        status: str | None = None,
    ):
        return await operate(
            "list_runs",
            records=True,
            limit=limit,
            cursor=cursor,
            page=page,
            session_id=session_id,
            status=status,
        )

    @router.get("/agent-runs/{execution_id}", response_model=RunDetail)
    async def detail(execution_id: str):
        return await operate("get_run", execution_id, records=True)

    @router.get("/agent-runs/{execution_id}/steps", response_model=CatalogPage[StepView])
    async def steps(
        execution_id: str,
        limit: int = Query(10, ge=1, le=100),
        cursor: str | None = Query(None, max_length=512),
        page: int | None = Query(None, ge=1),
    ):
        return await operate(
            "list_steps", execution_id, records=True, limit=limit, cursor=cursor, page=page
        )
