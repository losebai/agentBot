"""MySQL 与 SQLite 的知识新增、逻辑删除和有界检索事务。"""

from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from agent_bot.ai.contracts import Page
from agent_bot.ai.database import AiDatabase, page_limit
from agent_bot.ai.database import query as db_query
from agent_bot.ai.errors import AiError
from agent_bot.knowledge.contracts import (
    KnowledgeCount,
    KnowledgeCreate,
    KnowledgeMatch,
    KnowledgeSearchArguments,
    KnowledgeView,
    TagCount,
)
from agent_bot.knowledge.errors import KnowledgeError
from agent_bot.knowledge.pagination import decode_cursor, encode_cursor, literal_like

T = TypeVar("T")


def _knowledge_conditions(query: str, tag: str) -> tuple[list[str], list[Any]]:
    """列表与独立统计共用筛选，避免总数与页内容采用不同的删除或标签边界。"""
    conditions, args = ["k.is_deleted=0"], []
    if query:
        conditions.append("(k.title LIKE %s ESCAPE '!' OR k.content LIKE %s ESCAPE '!')")
        args.extend((literal_like(query), literal_like(query)))
    if tag:
        conditions.append(
            "EXISTS (SELECT 1 FROM chat_knowledge_tag t WHERE t.knowledge_id=k.id AND t.tag=%s)"
        )
        args.append(tag)
    return conditions, args


class KnowledgeStore(AiDatabase):
    """构造仅绑定已有连接池，普通启动不创建或升级知识表。"""

    async def run(self, operation: Callable[[Any], T], *, write: bool = False) -> T:
        """复用数据库收尾与连接释放，向外仅返回固定知识错误。"""
        try:
            return await super().run(operation, write=write)
        except KnowledgeError:
            raise
        except AiError as error:
            code = (
                "KNOWLEDGE_NOT_INITIALIZED"
                if error.code == "AI_NOT_INITIALIZED"
                else "KNOWLEDGE_CONFLICT"
                if error.status_code == 409
                else "KNOWLEDGE_UNAVAILABLE"
            )
            raise KnowledgeError(code, status_code=error.status_code) from error

    def _views(self, connection: Any, rows: list[dict]) -> tuple[KnowledgeView, ...]:
        """批量读取标签，列表与检索使用同一个事务快照。"""
        if not rows:
            return ()
        ids = tuple(row["id"] for row in rows)
        tags = db_query(
            connection,
            "SELECT knowledge_id,tag FROM chat_knowledge_tag WHERE "
            f"knowledge_id IN ({','.join('%s' for _ in ids)}) ORDER BY tag",
            ids,
        )
        grouped: dict[int, list[str]] = {id: [] for id in ids}
        for tag in tags:
            grouped[tag["knowledge_id"]].append(tag["tag"])
        return tuple(
            KnowledgeView(
                id=row["id"],
                title=row["title"],
                content=row["content"],
                tags=tuple(grouped[row["id"]]),
                created_at=row["create_time"],
            )
            for row in rows
        )

    async def create(self, body: KnowledgeCreate) -> KnowledgeView:
        """正文与主题原子新增，不存在更新原条目的路径。"""

        def insert(connection: Any) -> KnowledgeView:
            """标签写入失败时沿用事务回滚整个知识条目。"""
            now = datetime.now()
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT INTO chat_knowledge (title,content,create_time) VALUES (%s,%s,%s)",
                    (body.title, body.content, now),
                )
                id = cursor.lastrowid
                cursor.executemany(
                    "INSERT INTO chat_knowledge_tag (knowledge_id,tag) VALUES (%s,%s)",
                    [(id, tag) for tag in body.tags],
                )
            return KnowledgeView(**body.model_dump(), id=id, created_at=now)

        return await self.run(insert, write=True)

    async def get(self, id: int) -> KnowledgeView:
        """已删除条目不再提供可引用详情。"""

        def read(connection: Any) -> KnowledgeView:
            """正文与标签在一个有界事务内读取。"""
            row = db_query(
                connection,
                "SELECT * FROM chat_knowledge WHERE id=%s AND is_deleted=0",
                (id,),
                one=True,
            )
            if row is None:
                raise KnowledgeError("KNOWLEDGE_NOT_FOUND", status_code=404)
            return self._views(connection, [row])[0]

        return await self.run(read)

    async def list(
        self, *, limit: int = 20, cursor: str | None = None, query: str = "", tag: str = "",
        page: int | None = None,
    ) -> Page[KnowledgeView]:
        """兼容筛选绑定的旧游标与直接页码；分页请求不重复统计总数。"""
        page_limit(limit)
        if page is not None and (
            type(page) is not int or page < 1 or cursor is not None
        ):
            raise KnowledgeError("KNOWLEDGE_INVALID_PAGE", status_code=422)
        filters = {"query": query, "tag": tag}
        last_id = decode_cursor(cursor, filters)
        conditions, args = _knowledge_conditions(query, tag)
        if last_id is not None:
            conditions.append("k.id<%s")
            args.append(last_id)
        paging = (limit + 1, (page - 1) * limit) if page is not None else (limit + 1,)

        def read(connection: Any) -> Page[KnowledgeView]:
            """多读一条判断下一页，标签不改变正文分页结果。"""
            rows = db_query(
                connection,
                "SELECT k.* FROM chat_knowledge k WHERE "
                + " AND ".join(conditions)
                + " ORDER BY k.id DESC LIMIT %s"
                + (" OFFSET %s" if page is not None else ""),
                (*args, *paging),
            )
            items = self._views(connection, rows[:limit])
            return Page(
                items=items,
                next_cursor=encode_cursor(items[-1].id, filters) if len(rows) > limit else None,
            )

        return await self.run(read)

    async def count(self, *, query: str = "", tag: str = "") -> KnowledgeCount:
        """按当前筛选独立计数，不加载正文或全量标签目录。"""
        conditions, args = _knowledge_conditions(query, tag)
        row = await self.run(lambda connection: db_query(
            connection, "SELECT COUNT(*) AS total FROM chat_knowledge k WHERE "
            + " AND ".join(conditions), tuple(args), one=True,
        ))
        return KnowledgeCount.model_validate(row)

    async def tags(self) -> tuple[TagCount, ...]:
        """目录仅统计未删除知识，避免展示已不再可用的主题。"""
        return await self.run(
            lambda connection: tuple(
                TagCount.model_validate(row)
                for row in db_query(
                    connection,
                    "SELECT t.tag,COUNT(*) AS count FROM chat_knowledge_tag t "
                    "JOIN chat_knowledge k ON k.id=t.knowledge_id WHERE k.is_deleted=0 "
                    "GROUP BY t.tag ORDER BY count DESC,t.tag",
                )
            )
        )

    async def delete(self, id: int) -> None:
        """仅改变删除状态，原始正文和标签保持不变。"""

        def remove(connection: Any) -> None:
            """受影响行数同时处理并发删除和不存在的 ID。"""
            with connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE chat_knowledge SET is_deleted=1 WHERE id=%s AND is_deleted=0", (id,)
                )
                if cursor.rowcount == 0:
                    raise KnowledgeError("KNOWLEDGE_NOT_FOUND", status_code=404)

        await self.run(remove, write=True)

    async def search(self, arguments: KnowledgeSearchArguments) -> tuple[KnowledgeMatch, ...]:
        """标签任一命中与关键词取交集，固定数量与截断状态限制模型上下文。"""
        conditions, args = ["k.is_deleted=0"], []
        rank = "0"
        if arguments.tags:
            placeholders = ",".join("%s" for _ in arguments.tags)
            rank = (
                "(SELECT COUNT(*) FROM chat_knowledge_tag t WHERE t.knowledge_id=k.id "
                f"AND t.tag IN ({placeholders}))"
            )
            args.extend(arguments.tags)
            conditions.append(
                "EXISTS (SELECT 1 FROM chat_knowledge_tag t "
                f"WHERE t.knowledge_id=k.id AND t.tag IN ({placeholders}))"
            )
            args.extend(arguments.tags)
        if arguments.query:
            conditions.append("(k.title LIKE %s ESCAPE '!' OR k.content LIKE %s ESCAPE '!')")
            args.extend((literal_like(arguments.query), literal_like(arguments.query)))

        def read(connection: Any) -> tuple[KnowledgeMatch, ...]:
            """实际条目 ID 与标题作为引用来源，不生成虚构命中。"""
            rows = db_query(
                connection,
                f"SELECT k.*,{rank} AS matches FROM chat_knowledge k WHERE "
                + " AND ".join(conditions)
                + " ORDER BY matches DESC,k.id DESC LIMIT 5",
                tuple(args),
            )
            return tuple(
                KnowledgeMatch(
                    id=item.id,
                    title=item.title,
                    tags=item.tags,
                    content=item.content[:2000],
                    truncated=len(item.content) > 2000,
                )
                for item in self._views(connection, rows)
            )

        return await self.run(read)
