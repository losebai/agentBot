"""复用进程工作流存储提供管理功能，不在 HTTP 请求中执行结构迁移。"""

import asyncio
from dataclasses import asdict, replace
from pathlib import Path

from fastapi import APIRouter, Query, Request

from agent_bot.admin.models import SettingsUpdate
from agent_bot.admin.settings import SettingsService
from agent_bot.admin.workflow_models import (
    WorkflowCatalog,
    WorkflowDefaultUpdate,
    WorkflowDetail,
    WorkflowImport,
    WorkflowImportResult,
    WorkflowManagementError,
    WorkflowSummary,
    WorkflowUpdate,
)
from agent_bot.database_pool import finish_database_write
from agent_bot.integrations.workflow_store import WorkflowStore, workflow_revision
from agent_bot.services.workflows import (
    WorkflowCatalogImporter,
    WorkflowDefinition,
    WorkflowValidationError,
    validate_workflow_bindings,
)


def _summary(definition: WorkflowDefinition) -> WorkflowSummary:
    """列表剥离大体积正文，但保留执行绑定用于管理员定位不可执行原因。"""
    values = asdict(definition)
    values.pop("ui_json")
    values.pop("api_json")
    return WorkflowSummary(
        **values, executable=definition.executable, revision=workflow_revision(definition),
    )


def _detail(definition: WorkflowDefinition) -> WorkflowDetail:
    """详情与列表共用状态和版本，正文不会包含任何运行配置密钥。"""
    return WorkflowDetail(
        **_summary(definition).model_dump(),
        ui_json=definition.ui_json, api_json=definition.api_json,
    )


def install_workflow_routes(router: APIRouter, runtime, settings: SettingsService) -> None:
    """继承统一管理认证，静态同步目录只能读取当前已生效配置。"""

    def store() -> WorkflowStore:
        """延迟解析进程存储，不为未初始化的服务隐式创建工作流库。"""
        value = getattr(runtime, "workflow_store", None)
        if value is None:
            raise WorkflowManagementError("WORKFLOW_NOT_READY", status_code=503)
        return value

    def catalog(after: str | None = None) -> WorkflowCatalog:
        """返回当前目录和持久默认项；默认保存版本与应用版本由配置页区分。"""
        definitions, next_cursor = store().list_page(after=after)
        snapshot = settings.store.load()
        return WorkflowCatalog(
            items=[_summary(item) for item in definitions],
            default_key=snapshot.config.chat_local_workflow_default_key,
            settings_revision=snapshot.revision,
            directory_configured=bool(runtime.config.chat_local_workflow_directory),
            next_cursor=next_cursor,
        )

    @router.get("/workflows", response_model=WorkflowCatalog)
    async def list_workflows(
        after: str | None = Query(default=None, pattern=r"^[a-z0-9][a-z0-9-]{0,127}$"),
    ) -> WorkflowCatalog:
        """读取数据库目录，不访问 Comfy 服务或扫描磁盘。"""
        async with runtime.lease():
            return await asyncio.to_thread(catalog, after)

    @router.post("/workflows/import", response_model=WorkflowImportResult)
    async def import_workflow(request: Request) -> WorkflowImportResult:
        """认证和插件门槛通过后解析有界正文；导入不落文件或解析伴随路径。"""
        async with runtime.lease():
            target_store = store()
            body = WorkflowImport.model_validate_json(await request.body())

            def persist() -> WorkflowImportResult:
                """转换和事务都在线程执行，取消时仍等待已启动写入完成。"""
                definition = WorkflowCatalogImporter().parse_json(body.filename, body.content)
                definition = replace(
                    definition,
                    enabled=definition.executable and definition.workflow_type != "image_to_video",
                )
                return WorkflowImportResult(imported=target_store.upsert((definition,)))

            return await finish_database_write(persist)

    @router.post("/workflows/sync", response_model=WorkflowImportResult)
    async def sync_workflows() -> WorkflowImportResult:
        """先验证插件运行能力再扫描目录，整批同步技术字段并保留管理员信息。"""
        async with runtime.lease():
            target_store = store()
            directory = runtime.config.chat_local_workflow_directory
            if not directory:
                raise WorkflowManagementError("WORKFLOW_DIRECTORY_MISSING", status_code=422)

            def persist() -> WorkflowImportResult:
                """HTTP 同步沿用 CLI 导入协议，缺表明确报错，不执行 migrate。"""
                try:
                    definitions = WorkflowCatalogImporter().scan(Path(directory))
                except OSError as exc:
                    raise WorkflowManagementError(
                        "WORKFLOW_DIRECTORY_UNAVAILABLE", status_code=422,
                    ) from exc
                return WorkflowImportResult(imported=target_store.upsert(definitions))

            return await finish_database_write(persist)

    @router.get("/workflows/{workflow_key}", response_model=WorkflowDetail)
    async def workflow_detail(workflow_key: str) -> WorkflowDetail:
        """插件租约保护读取；停用的模板仍可查看，但停用插件不开放存储。"""
        async with runtime.lease():
            return _detail(await asyncio.to_thread(store().get, workflow_key))

    @router.patch("/workflows/{workflow_key}", response_model=WorkflowDetail)
    async def update_workflow(workflow_key: str, body: WorkflowUpdate) -> WorkflowDetail:
        """租约防止退出关闭存储，行锁检查摘要版本，不允许网页覆盖技术字段。"""
        async with runtime.lease():
            target_store = store()
            definition = await finish_database_write(lambda: target_store.update_metadata(
                workflow_key, expected_revision=body.expected_revision, values=body.values,
            ))
            return _detail(definition)

    @router.post("/workflows/{workflow_key}/default", response_model=WorkflowCatalog)
    async def set_default(workflow_key: str, body: WorkflowDefaultUpdate) -> WorkflowCatalog:
        """先验证可执行且已启用，再通过配置服务保存默认键，沿用后台热刷新。"""
        def persist() -> WorkflowCatalog:
            """复用完整配置校验与乐观锁；不绕过现有设置的静态写入边界。"""
            definition = store().get(workflow_key)
            if definition.workflow_type == "image_to_video":
                raise WorkflowValidationError("video workflow cannot be the default image workflow")
            if not definition.enabled:
                raise WorkflowValidationError("default workflow is disabled")
            validate_workflow_bindings(definition)
            settings.update(SettingsUpdate(
                expected_revision=body.expected_revision,
                values={"chat_local_workflow_default_key": workflow_key},
            ))
            return catalog()

        async with runtime.lease():
            return await finish_database_write(persist)
