"""管理认证后的 AI 动态目录、模型发现和执行记录接口。"""

from fastapi import APIRouter, Query

from agent_bot.ai.call_records import (
    CallCategory,
    CallDetail,
    CallRecordStore,
    CallSummary,
    CallType,
    SystemKind,
)
from agent_bot.ai.contracts import (
    ModelCreate,
    ModelImportRequest,
    ModelImportResult,
    ModelUpdate,
    ModelView,
    Page,
    ProviderCreate,
    ProviderUpdate,
    ProviderView,
    SessionDetail,
    SessionSummary,
    UpstreamModels,
    VersionDelete,
)
from agent_bot.ai.errors import AiError


def install_ai_routes(router: APIRouter, runtime) -> None:
    """绑定当前运行时服务，目录响应不公开密钥。"""

    async def call_records(action, **kwargs):
        """统一查询仍持有运行时租约并沿用已认证路由。"""
        async with runtime.lease():
            service = getattr(runtime, "ai_service", None)
            if service is None:
                raise AiError("AI_NOT_INITIALIZED", status_code=503)
            sessions = service.sessions
            store = CallRecordStore(sessions.params, connect=sessions.connect)
            return await getattr(store, action)(**kwargs)

    @router.get("/ai/records", response_model=Page[CallSummary])
    async def unified_records(
        type: CallType | None = None,
        category: CallCategory | None = None,
        system_kind: SystemKind | None = None,
        session_id: str | None = Query(None, max_length=255),
        status: str | None = Query(None, max_length=16),
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = Query(None, max_length=2048),
    ):
        """按普通或系统调用分组查询，类型、会话和游标一起约束结果。"""
        return await call_records(
            "list_calls",
            type=type,
            category=category,
            system_kind=system_kind,
            session_id=session_id,
            status=status,
            limit=limit,
            cursor=cursor,
        )

    @router.get("/ai/records/{id}", response_model=CallDetail)
    async def unified_detail(id: int):
        """统一详情保留旧字段名，避免混淆输入与模型回复。"""
        return await call_records("get_call", id=id)

    async def operate(action: str, *args, records: bool = False, **kwargs):
        """读写操作持有运行时租约，返回仅序列化公开字段的快照。"""
        async with runtime.lease():
            service = getattr(runtime, "ai_service", None)
            if service is None:
                raise AiError("AI_NOT_INITIALIZED", status_code=503)
            result = await getattr(service.sessions if records else service.catalog, action)(
                *args, **kwargs
            )

            return result

    @router.get("/ai/providers", response_model=Page[ProviderView])
    async def providers(limit: int = Query(50, ge=1, le=100), cursor: str | None = None):
        return await operate("list_providers", limit, cursor)

    @router.post("/ai/providers", response_model=ProviderView, status_code=201)
    async def create_provider(body: ProviderCreate):
        return await operate("create_provider", body)

    @router.patch("/ai/providers/{id}", response_model=ProviderView)
    async def update_provider(id: int, body: ProviderUpdate):
        return await operate("update_provider", id, body)

    @router.delete("/ai/providers/{id}", status_code=204)
    async def delete_provider(id: int, body: VersionDelete):
        await operate("delete_provider", id, body.expected_revision)

    @router.get("/ai/models", response_model=Page[ModelView])
    async def models(
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = None,
        provider_id: int | None = Query(None, ge=1),
    ):
        """按供应商显示本地登记状态，同名上游模型分别处理。"""
        return await operate("list_models", limit, cursor, provider_id)

    @router.get("/ai/providers/{id}/upstream-models", response_model=UpstreamModels)
    async def upstream_models(id: int, expected_revision: int = Query(..., ge=1)):
        """管理员只选择登记的供应商，密钥在服务器端读取。"""
        async with runtime.lease():
            service = getattr(runtime, "ai_service", None)
            if service is None:
                raise AiError("AI_NOT_INITIALIZED", status_code=503)
            return await service.upstream_models(id, expected_revision)

    @router.post("/ai/providers/{id}/models/import", response_model=ModelImportResult)
    async def import_models(id: int, body: ModelImportRequest):
        """导入只能绑定路径中的供应商，不能通过正文转移关联。"""
        return await operate("import_models", id, body)

    @router.post("/ai/models", response_model=ModelView, status_code=201)
    async def create_model(body: ModelCreate):
        return await operate("create_model", body)

    @router.patch("/ai/models/{id}", response_model=ModelView)
    async def update_model(id: int, body: ModelUpdate):
        return await operate("update_model", id, body)

    @router.delete("/ai/models/{id}", status_code=204)
    async def delete_model(id: int, body: VersionDelete):
        await operate("delete_model", id, body.expected_revision)

    @router.get("/ai/sessions", response_model=Page[SessionSummary])
    async def sessions(
        session_id: str | None = Query(None, max_length=256),
        status: str | None = None,
        limit: int = Query(50, ge=1, le=100),
        cursor: str | None = Query(None, max_length=512),
    ):
        return await operate(
            "list_records",
            records=True,
            session_id=session_id,
            status=status,
            limit=limit,
            cursor=cursor,
        )

    @router.get("/ai/sessions/{execution_id}", response_model=SessionDetail)
    async def record(execution_id: str):
        return await operate("get_record", execution_id, records=True)
