"""已认证管理入口的知识维护和记忆版本、来源与预览批次管理。"""

from typing import Literal

from fastapi import APIRouter, Path, Query

from agent_bot.ai.contracts import Page
from agent_bot.database_pool import finish_database_write
from agent_bot.integrations.tagged_memory_store import (
    TaggedMemoryCapacityError,
    TaggedMemoryConflictError,
    TaggedMemoryStoreError,
)
from agent_bot.knowledge.contracts import (
    KnowledgeCreate,
    KnowledgeView,
    MemoryCreate,
    MemoryRevisionView,
    MemoryView,
    ScopeView,
    TagCount,
)
from agent_bot.knowledge.errors import KnowledgeError
from agent_bot.memory.contracts import (
    MemoryCleanup,
    MemoryCleanupPreview,
    MemoryKind,
    MemoryMerge,
    MemoryResolve,
    MemoryStatus,
    MemoryUpdate,
)
from agent_bot.memory.policy import normalized_content
from agent_bot.services.tagged_memory import MemoryScope, normalize_memory_tags


def normalized_tag(tag: str) -> str:
    """管理筛选与新增使用同一标签标准，非法输入返回有界错误。"""
    if not tag:
        return ""
    try:
        return normalize_memory_tags((tag,))[0]
    except ValueError as error:
        raise KnowledgeError("KNOWLEDGE_INVALID_INPUT", status_code=422) from error


def install_knowledge_routes(router: APIRouter, runtime) -> None:
    """知识保持只增只读，记忆修订与批量操作在宿主租约内完成事务收尾。"""

    async def knowledge(action: str, *args, **kwargs):
        """不依赖 QQ 插件和机器人连接，不缓存过期配置快照。"""
        async with runtime.lease():
            store = getattr(runtime, "knowledge_store", None)
            if store is None:
                raise KnowledgeError("KNOWLEDGE_UNAVAILABLE", status_code=503)
            return await getattr(store, action)(*args, **kwargs)

    async def memory(action: str, *args, **kwargs):
        """管理读取附加目录标题，取消时等全部工作线程归还连接再释放运行时租约。"""
        async with runtime.lease():
            store = getattr(runtime, "tagged_memory_store", None)
            if store is None:
                raise KnowledgeError("MEMORY_UNAVAILABLE", status_code=503)

            def operate():
                """标题只装饰管理响应，QQ 记忆调用与不可逆权限键保持原有边界。"""
                result = getattr(store, action)(*args, **kwargs)
                preview = action in {"admin_merge_preview", "admin_cleanup_preview"}
                if action not in {
                    "admin_list", "admin_get", "admin_create", "admin_update", "admin_merge",
                    "admin_scopes", "admin_merge_preview", "admin_cleanup_preview",
                    "admin_conflicts", "admin_resolve",
                }:
                    return result
                rows = (result["items"] if preview else result.items
                        if isinstance(result, Page) else result
                        if action == "admin_scopes" else (result,))
                if not rows:
                    return result
                titles = store.admin_scope_titles({row.scope_id: row.scope for row in rows})
                enriched = [row.model_copy(update={"scope_title": titles.get(row.scope_id)})
                            for row in rows]
                if isinstance(result, Page):
                    return result.model_copy(update={"items": enriched})
                if preview:
                    # 预览摘要只绑定事实与版本，目录标题不参与写入身份或摘要计算。
                    return {**result, "items": enriched}
                return tuple(enriched) if action == "admin_scopes" else enriched[0]

            try:
                return await finish_database_write(operate)
            except TaggedMemoryConflictError as error:
                code = "MEMORY_CONFLICT" if action == "admin_create" else "MEMORY_REVISION_CONFLICT"
                raise KnowledgeError(code, status_code=409) from error
            except TaggedMemoryCapacityError as error:
                raise KnowledgeError("MEMORY_CAPACITY", status_code=409) from error
            except TaggedMemoryStoreError as error:
                raise KnowledgeError("MEMORY_UNAVAILABLE", status_code=503) from error
            except ValueError as error:
                raise KnowledgeError("MEMORY_INVALID_INPUT", status_code=422) from error

    @router.get("/knowledge", response_model=Page[KnowledgeView])
    async def list_knowledge(
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=4096),
        query: str = Query("", max_length=200),
        tag: str = Query("", max_length=64),
    ):
        """筛选先于分页，规范化标签后再绑定游标。"""
        return await knowledge(
            "list", limit=limit, cursor=cursor, query=query.strip(), tag=normalized_tag(tag)
        )

    @router.post("/knowledge", response_model=KnowledgeView, status_code=201)
    async def create_knowledge(body: KnowledgeCreate):
        """只有新增，没有借新增覆写既有 ID 的字段。"""
        return await knowledge("create", body)

    @router.get("/knowledge/{id}", response_model=KnowledgeView)
    async def get_knowledge(id: int = Path(ge=1)):
        """删除后详情不再开放。"""
        return await knowledge("get", id)

    @router.delete("/knowledge/{id}", status_code=204)
    async def delete_knowledge(id: int = Path(ge=1)):
        """只隐藏目标条目并保留原文。"""
        await knowledge("delete", id)

    @router.get("/knowledge-tags", response_model=tuple[TagCount, ...])
    async def knowledge_tags():
        """仅返回有效知识的主题计数。"""
        return await knowledge("tags")

    @router.get("/memories", response_model=Page[MemoryView])
    async def list_memories(
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=4096),
        query: str = Query("", max_length=200),
        tag: str = Query("", max_length=64),
        scope: MemoryScope | None = None,
        scope_id: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
        topic: str = Query("", max_length=80),
        status: MemoryStatus | Literal["future", "all"] | None = None,
        kind: MemoryKind | None = None,
        subject: str | None = Query(None, max_length=128),
    ):
        """管理员筛选匿名归属，不把管理范围授予 QQ 召回。"""
        return await memory(
            "admin_list",
            limit=limit,
            cursor=cursor,
            query=query.strip(),
            tag=normalized_tag(tag),
            scope=scope,
            scope_id=scope_id,
            topic=normalized_content(topic).casefold(),
            status=status,
            kind=kind,
            subject=subject,
        )

    @router.post("/memories", response_model=MemoryView, status_code=201)
    async def create_memory(body: MemoryCreate):
        """重复事实由存储去重，已有键冲突不通过换键或恢复绕开。"""
        return await memory("admin_create", body)

    @router.post("/memories/merge-preview")
    async def preview_memory_merge(body: MemoryMerge):
        """只预览显式目标版本，授权管理能力不扩大 QQ 工具的可见范围。"""
        return await memory("admin_merge_preview", body)

    @router.post("/memories/merge", response_model=MemoryView)
    async def merge_memories(body: MemoryMerge):
        """服务端校验预览摘要后原子修订目标和来源墓碑。"""
        if body.digest is None:
            raise KnowledgeError("MEMORY_INVALID_INPUT", status_code=422)
        return await memory("admin_merge", body)

    @router.post("/memories/cleanup-preview")
    async def preview_memory_cleanup(body: MemoryCleanupPreview):
        """主题仅用于有界预览，不能作为正式清理时的动态条件。"""
        normalized = body.model_copy(update={"topic": normalized_content(body.topic).casefold()})
        return await memory("admin_cleanup_preview", normalized)

    @router.post("/memories/cleanup")
    async def cleanup_memories(body: MemoryCleanup):
        """只处理预览列出的 ID 和版本，随后新增的同主题事实保持原状。"""
        return await memory("admin_cleanup", body)

    @router.post("/memories/resolve", response_model=MemoryView)
    async def resolve_memories(body: MemoryResolve):
        """同时校验整组冲突版本，采用目标并原子失效其他结论。"""
        return await memory("admin_resolve", body)

    @router.get("/memories/{id}", response_model=MemoryView)
    async def get_memory(id: int = Path(ge=1)):
        """读取当前事实及可验证来源，遗忘记录不通过普通详情返回。"""
        return await memory("admin_get", id)

    @router.patch("/memories/{id}", response_model=MemoryView)
    async def update_memory(body: MemoryUpdate, id: int = Path(ge=1)):
        """保留未提交字段；陈旧版本返回冲突而不自动覆盖当前事实。"""
        return await memory("admin_update", id, body)

    @router.get("/memories/{id}/revisions", response_model=Page[MemoryRevisionView])
    async def memory_revisions(
        id: int = Path(ge=1),
        limit: int = Query(20, ge=1, le=100),
        cursor: str | None = Query(None, max_length=4096),
    ):
        """版本原文仅供管理认证审计，不暴露给 QQ 历史读取工具。"""
        return await memory("admin_revisions", id, limit=limit, cursor=cursor)

    @router.get("/memories/{id}/conflicts", response_model=Page[MemoryView])
    async def memory_conflicts(id: int = Path(ge=1)):
        """整组返回有界冲突候选，管理员必须核对全部受影响版本。"""
        return await memory("admin_conflicts", id)

    @router.delete("/memories/{id}", status_code=204)
    async def delete_memory(
        id: int = Path(ge=1), expected_revision: int = Query(..., ge=1),
    ):
        """遗忘绑定已看到的版本并保留审计历史，不提供自动恢复。"""
        await memory("admin_delete", id, expected_revision=expected_revision)

    @router.get("/memory-tags", response_model=tuple[TagCount, ...])
    async def memory_tags(
        scope: MemoryScope | None = None,
        scope_id: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
    ):
        """标签统计只包含当前作用域中的有效记忆。"""
        return await memory("admin_tags", scope=scope, scope_id=scope_id)

    @router.get("/memory-scopes", response_model=tuple[ScopeView, ...])
    async def memory_scopes(scope: MemoryScope | None = None):
        """已认证管理员可用 QQ 昵称和群名识别归属，筛选仍使用不可逆键。"""
        return await memory("admin_scopes", scope=scope)
