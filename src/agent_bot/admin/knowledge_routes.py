"""已认证管理入口的知识与标签记忆新增、读取和逻辑删除。"""

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
    MemoryView,
    ScopeView,
    TagCount,
)
from agent_bot.knowledge.errors import KnowledgeError
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
    """只安装 GET/POST/DELETE，读写均在宿主租约内完成连接收尾。"""

    async def knowledge(action: str, *args, **kwargs):
        """不依赖 QQ 插件和机器人连接，不缓存过期配置快照。"""
        async with runtime.lease():
            store = getattr(runtime, "knowledge_store", None)
            if store is None:
                raise KnowledgeError("KNOWLEDGE_UNAVAILABLE", status_code=503)
            return await getattr(store, action)(*args, **kwargs)

    async def memory(action: str, *args, **kwargs):
        """取消时等工作线程归还连接，避免先释放运行时租约。"""
        async with runtime.lease():
            store = getattr(runtime, "tagged_memory_store", None)
            if store is None:
                raise KnowledgeError("MEMORY_UNAVAILABLE", status_code=503)
            try:
                return await finish_database_write(lambda: getattr(store, action)(*args, **kwargs))
            except TaggedMemoryConflictError as error:
                raise KnowledgeError("MEMORY_CONFLICT", status_code=409) from error
            except TaggedMemoryCapacityError as error:
                raise KnowledgeError("MEMORY_CAPACITY", status_code=409) from error
            except TaggedMemoryStoreError as error:
                raise KnowledgeError("MEMORY_UNAVAILABLE", status_code=503) from error

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
        )

    @router.post("/memories", response_model=MemoryView, status_code=201)
    async def create_memory(body: MemoryCreate):
        """同作用域同键统一返回冲突，已删除键也不能恢复。"""
        return await memory("admin_create", body)

    @router.get("/memories/{id}", response_model=MemoryView)
    async def get_memory(id: int = Path(ge=1)):
        """详情保留正文，不提供更新接口。"""
        return await memory("admin_get", id)

    @router.delete("/memories/{id}", status_code=204)
    async def delete_memory(id: int = Path(ge=1)):
        """明确逻辑删除，底层不会改写正文和标签。"""
        await memory("admin_delete", id)

    @router.get("/memory-tags", response_model=tuple[TagCount, ...])
    async def memory_tags(
        scope: MemoryScope | None = None,
        scope_id: str | None = Query(None, pattern=r"^[a-f0-9]{64}$"),
    ):
        """标签统计只包含当前作用域中的有效记忆。"""
        return await memory("admin_tags", scope=scope, scope_id=scope_id)

    @router.get("/memory-scopes", response_model=tuple[ScopeView, ...])
    async def memory_scopes(scope: MemoryScope | None = None):
        """作用域目录仅包含不可逆键和类型。"""
        return await memory("admin_scopes", scope=scope)
