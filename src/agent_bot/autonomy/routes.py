"""管理员持续智能体接口，全部沿用外层认证和当前运行时租约。"""

from contextlib import asynccontextmanager

from fastapi import APIRouter, Query

from agent_bot.ai.errors import AiError
from agent_bot.autonomy.contracts import (
    Decision,
    EpisodeCreate,
    LocalMessage,
    ProfileCreate,
    ProfileUpdate,
    TaskCreate,
)
from agent_bot.autonomy.service import AutonomyService
from agent_bot.autonomy.store import AutonomyStore
from agent_bot.evolution.contracts import EvolutionAdvance


def install_autonomy_routes(router: APIRouter, runtime) -> None:
    """只提供数据及意图操作，不提供进程启动、任意命令或未经授权的远程工具执行。"""

    @asynccontextmanager
    async def service(*, read_only=False):
        """从已持有的记忆连接池构造薄存储适配器，不创建第二套运行时或配置权威。"""
        async with runtime.lease(read_only=read_only):
            memories = getattr(runtime, "tagged_memory_store", None)
            if memories is None:
                raise AiError("AUTONOMY_UNAVAILABLE", status_code=503)
            store = AutonomyStore(
                memories._params,
                connect=memories._connect,
                secret=runtime.config.chat_session_secret.get_secret_value(),
                local_only=getattr(runtime, "local_only", False),
            )
            yield AutonomyService(store, memories)

    @router.get("/autonomy/profiles")
    async def profiles(after: str = "", limit: int = Query(50, ge=1, le=100)):
        """稳定主键分页查询当前身份命名空间。"""
        async with service(read_only=True) as agent:
            return await agent.store.profiles(after=after, limit=limit)

    @router.post("/autonomy/profiles", status_code=201)
    async def create_profile(body: ProfileCreate):
        """创建明确可信绑定，默认暂停；相同所有者重复创建保留原身份。"""
        async with service() as agent:
            return await agent.store.create_profile(body)

    @router.post("/autonomy/pause", status_code=204)
    async def pause_all():
        """无需版本号紧急暂停当前密钥空间全部持续身份。"""
        async with service() as agent:
            await agent.store.pause()

    @router.get("/autonomy/profiles/{id}")
    async def profile(id: str):
        """分别读取设定、状态、宿主策略与实际预算计数。"""
        async with service(read_only=True) as agent:
            return await agent.store.get_profile(id)

    @router.patch("/autonomy/profiles/{id}")
    async def update_profile(id: str, body: ProfileUpdate):
        """所见版本校验防止旧页面覆盖已生效的身份、边界或暂停开关。"""
        async with service() as agent:
            return await agent.store.update_profile(id, body)

    @router.post("/autonomy/profiles/{id}/pause", status_code=204)
    async def pause_profile(id: str):
        """单身份紧急暂停不依赖读到的旧版本。"""
        async with service() as agent:
            await agent.store.pause(id)

    @router.post("/autonomy/profiles/{id}/events", status_code=201)
    async def ingest_event(id: str, body: EpisodeCreate):
        """提交真实来源事件；显式任务意图与资料引用分开校验。"""
        async with service() as agent:
            return await agent.store.ingest(id, body)

    @router.post('/autonomy/profiles/{id}/messages', status_code=201)
    async def send_message(id: str, body: LocalMessage):
        """本地独立对话入队，宿主绑定来源与时间；HTTP不调用模型或平台适配器。"""
        async with service() as agent:
            return await agent.store.send_message(id, body.request_id, body.text)

    @router.get("/autonomy/profiles/{id}/memories")
    async def memories(id: str, query: str = Query("记忆", min_length=1, max_length=200)):
        """分层检索当前授权情节、事实、关系及任务，返回真实命中的来源与版本。"""
        async with service(read_only=True) as agent:
            return await agent.memory_view(id, query=query)

    @router.get("/autonomy/profiles/{id}/tasks")
    async def tasks(id: str, after: str = "", limit: int = Query(50, ge=1, le=100)):
        """任务状态展示检查点、在途事项和恢复原因。"""
        async with service(read_only=True) as agent:
            return await agent.store.tasks(id, after=after, limit=limit)

    @router.post("/autonomy/profiles/{id}/tasks", status_code=201)
    async def create_task(id: str, body: TaskCreate):
        """保存一次性或定时目标，不在HTTP请求中执行模型或等待整个计划。"""
        async with service() as agent:
            return await agent.store.create_task(id, body)

    @router.post("/autonomy/profiles/{id}/tasks/{task_id}/decision")
    async def decide_task(id: str, task_id: str, body: Decision):
        """审核当前计划报告权限或取消任务，审批不能绕过动作白名单。"""
        async with service() as agent:
            return await agent.store.decide_task(id, task_id, body.expected_revision, body.approve)

    @router.get("/autonomy/profiles/{id}/proposals")
    async def proposals(id: str, after: str = "", limit: int = Query(50, ge=1, le=100)):
        """未确认语义结论独立于长期事实列表，管理员可核对来源和预计覆盖版本。"""
        async with service(read_only=True) as agent:
            return await agent.store.proposals(id, after=after, limit=limit)

    @router.post("/autonomy/profiles/{id}/proposals/{proposal_id}/decision", status_code=204)
    async def decide_proposal(id: str, proposal_id: str, body: Decision):
        """审批固定提案版本，批准后交由后台复核来源和原事实版本再执行。"""
        async with service() as agent:
            await agent.store.decide_proposal(id, proposal_id, body.expected_revision, body.approve)

    @router.get("/autonomy/profiles/{id}/audit")
    async def audit(id: str, before: str | None = None, limit: int = Query(50, ge=1, le=100)):
        """审计包含身份版本、步骤状态、报告和评估，支持稳定倒序分页。"""
        async with service(read_only=True) as agent:
            return await agent.store.audit(id, before=before, limit=limit)

    @router.get("/autonomy/profiles/{id}/conversations")
    async def conversations(
        id: str, before: str | None = None, limit: int = Query(20, ge=1, le=50),
    ):
        """只读浏览来源、目标和持久化步骤结果，不触发推理或启动自主循环。"""
        async with service(read_only=True) as agent:
            return await agent.store.conversations(id, before=before, limit=limit)

    @router.get('/autonomy/profiles/{id}/evolution')
    async def evolution_preview(id: str):
        """独立Agent按自身真实反馈研究演进，不需要角色或平台账户。"""
        from agent_bot.evolution.store import EvolutionStore

        async with service(read_only=True) as agent:
            profile_id = await agent.store.evolution_profile(id)
            if profile_id is None:
                return {'research': None}
            evolution = EvolutionStore(agent.store.params, connect=agent.store.connect)
            return {'research': await evolution.preview(profile_id)}

    @router.post('/autonomy/profiles/{id}/evolution')
    async def evolution_advance(id: str, body: EvolutionAdvance):
        """应用自身研究快照，不能由网页提交其他演进画像ID。"""
        from agent_bot.evolution.store import EvolutionStore

        async with service() as agent:
            profile_id = await agent.store.evolution_profile(id)
            if profile_id is None:
                raise AiError('AUTONOMY_NOT_FOUND', status_code=404)
            evolution = EvolutionStore(agent.store.params, connect=agent.store.connect)
            return await evolution.advance_profile(profile_id, **body.model_dump())

    @router.post("/autonomy/profiles/{id}/evaluate")
    async def evaluate(id: str):
        """读取当前行为指标并运行无外联的固定结构评测，结果写审计。"""
        from agent_bot.autonomy.evaluation import evaluate_profile

        async with service() as agent:
            return await evaluate_profile(agent.store, id)
