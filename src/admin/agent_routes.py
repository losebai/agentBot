"""已认证的 Agent 目录、执行和记录管理接口。"""

from contextlib import aclosing

from fastapi import APIRouter, Query, Request

from agent_bot.admin.agent_chat import AgentChatProbe, AgentProbeRequest
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
)
from agent_bot.agent.errors import AgentError
from agent_bot.ai.contracts import Page, VersionDelete


def install_agent_routes(router: APIRouter, runtime):
    """安装 Agent 管理接口，执行时从当前租约获取服务和授权。"""
    chat = AgentChatProbe(runtime)

    async def operate(action, *args, records=False, **kwargs):
        async with runtime.lease():
            service = chat.service()
            result = await getattr(service.records if records else service.catalog, action)(
                *args, **kwargs
            )

            return result

    @router.get("/agents", response_model=Page[AgentView])
    async def list_agents(
        limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, max_length=512)
    ):
        return await operate("list_agents", limit=limit, cursor=cursor)

    @router.get("/agents/{id}", response_model=AgentView)
    async def get_agent(id: int):
        return await operate("get_agent", id)

    @router.post("/agents", response_model=AgentView, status_code=201)
    async def create_agent(body: AgentCreate):
        return await operate("create_agent", body)

    @router.patch("/agents/{id}", response_model=AgentView)
    async def update_agent(id: int, body: AgentUpdate):
        return await operate("update_agent", id, body)

    @router.delete("/agents/{id}", status_code=204)
    async def delete_agent(id: int, body: VersionDelete):
        await operate("delete_agent", id, body.expected_revision)

    @router.get("/skills", response_model=Page[SkillView])
    async def list_skills(
        limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, max_length=512)
    ):
        return await operate("list_skills", limit=limit, cursor=cursor)

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

    @router.get("/mcp-servers", response_model=Page[McpView])
    async def list_mcps(
        limit: int = Query(50, ge=1, le=100), cursor: str | None = Query(None, max_length=512)
    ):
        return await operate("list_mcps", limit=limit, cursor=cursor)

    @router.get("/mcp-servers/{id}", response_model=McpView)
    async def get_mcp(id: int):
        return await operate("get_mcp", id)

    @router.post("/mcp-servers", response_model=McpView, status_code=201)
    async def create_mcp(body: McpCreate):
        return await operate("create_mcp", body)

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

    @router.get("/agent-runs", response_model=Page[RunSummary])
    async def runs(
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = Query(None, max_length=1024),
        session_id: str | None = Query(None, max_length=256),
        status: str | None = None,
    ):
        return await operate(
            "list_runs",
            records=True,
            limit=limit,
            cursor=cursor,
            session_id=session_id,
            status=status,
        )

    @router.get("/agent-runs/{execution_id}", response_model=RunDetail)
    async def detail(execution_id: str):
        return await operate("get_run", execution_id, records=True)

    @router.get("/agent-runs/{execution_id}/steps", response_model=Page[StepView])
    async def steps(
        execution_id: str,
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = Query(None, max_length=512),
    ):
        return await operate("list_steps", execution_id, records=True, limit=limit, cursor=cursor)
