"""MySQL 或 SQLite 中标签记忆、授权作用域及关联的事务边界。"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from typing import Any, cast

import pymysql

from agent_bot.ai.contracts import Page
from agent_bot.ai.database import page_limit
from agent_bot.database import database_connector
from agent_bot.knowledge.contracts import MemoryCreate, MemoryView, ScopeView, TagCount
from agent_bot.knowledge.errors import KnowledgeError
from agent_bot.knowledge.pagination import decode_cursor, encode_cursor, literal_like
from agent_bot.services.tagged_memory import (
    MemoryArguments,
    MemoryIdentity,
    MemoryRecord,
    MemoryResult,
    MemoryScope,
    MemoryTag,
    memory_scope_owner,
)
from agent_bot.settings import DatabaseParams

SCHEMA_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS chat_memory_scope (
        scope_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '匿名作用域键',
        scope VARCHAR(16) NOT NULL COMMENT 'global/session/user/group',
        PRIMARY KEY (scope_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='标签记忆作用域与写锁'""",
    """CREATE TABLE IF NOT EXISTS chat_tagged_memory (
        id INT NOT NULL AUTO_INCREMENT COMMENT '记忆自增主键',
        scope_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '关联作用域',
        memory_key VARCHAR(80) COLLATE utf8mb4_bin NOT NULL COMMENT '稳定事实键',
        content VARCHAR(1000) NOT NULL COMMENT '已确认的事实',
        is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '逻辑删除标记',
        create_time DATETIME NOT NULL COMMENT '创建时间',
        update_time DATETIME NOT NULL COMMENT '更新时间',
        PRIMARY KEY (id),
        UNIQUE KEY uk_tagged_memory_scope_key (scope_id, memory_key),
        KEY idx_tagged_memory_scope_time (scope_id, is_deleted, update_time)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LLM 自主保存的标签记忆'""",
    """CREATE TABLE IF NOT EXISTS chat_memory_tag (
        memory_id INT NOT NULL COMMENT '关联记忆',
        tag VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '规范化主题标签',
        PRIMARY KEY (memory_id, tag),
        KEY idx_memory_tag_lookup (tag, memory_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆与标签多对多关联'""",
)


class TaggedMemoryStoreError(RuntimeError):
    """可对外返回的固定数据库边界错误。"""


class TaggedMemoryConflictError(TaggedMemoryStoreError):
    """已有键即使已删除也不能覆盖，调用方须明确创建另一条事实。"""


class TaggedMemoryCapacityError(TaggedMemoryStoreError):
    """作用域已满，只能明确遗忘旧事实后新增另一键。"""


class TaggedMemoryStore:
    """只访问绑定身份可见的作用域，单作用域最多 256 条记忆。"""

    def __init__(
        self,
        params: DatabaseParams,
        *,
        secret: str,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        """保存共享连接池入口及匿名作用域密钥。"""
        self._params = params
        self._secret = secret
        self._connect = connect or database_connector(params)

    @contextmanager
    def _transaction(self) -> Iterator[Any]:
        """关闭连接前明确提交或回滚，异常不泄露数据库细节。"""
        try:
            with closing(
                self._connect(
                    host=self._params.host,
                    port=self._params.port,
                    user=self._params.user,
                    password=self._params.password,
                    database=self._params.database,
                    charset="utf8mb4",
                    cursorclass=pymysql.cursors.DictCursor,
                    autocommit=False,
                    connect_timeout=10,
                    read_timeout=10,
                    write_timeout=10,
                )
            ) as connection:
                try:
                    with connection.cursor() as cursor:
                        yield cursor
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
        except pymysql.MySQLError as error:
            raise TaggedMemoryStoreError("记忆数据库暂时不可用") from error

    def initialize(self) -> None:
        """幂等创建独立表，使用应用事务维护关联，不添加外键。"""
        with self._transaction() as cursor:
            if self._params.backend == "sqlite":
                # 启动只检查手动初始化的 SQLite 表，避免隐式创建或覆盖业务结构。
                self._validate_fields(cursor)
                return
            for statement in SCHEMA_STATEMENTS:
                cursor.execute(statement)

    def validate(self) -> None:
        """宿主启动只校验现有字段，不为已有部署自动建表或升级。"""
        with self._transaction() as cursor:
            self._validate_fields(cursor)

    @staticmethod
    def _validate_fields(cursor: Any) -> None:
        """两种后端和显式初始化复用同一字段检查，不写入业务数据。"""
        cursor.execute("SELECT scope_id,scope FROM chat_memory_scope LIMIT 0")
        cursor.execute("SELECT id,scope_id,memory_key,content,is_deleted,create_time,"
                       "update_time FROM chat_tagged_memory LIMIT 0")
        cursor.execute("SELECT memory_id,tag FROM chat_memory_tag LIMIT 0")

    def execute(
        self,
        identity: MemoryIdentity,
        arguments: MemoryArguments,
        deadline_at: float | None = None,
    ) -> MemoryResult:
        """按受限操作路由，读写均受授权作用域与原请求截止时间限制。"""
        self._check_deadline(deadline_at)
        scopes = (
            identity.scopes if arguments.scope == "all" else (cast(MemoryScope, arguments.scope),)
        )
        owners = tuple(identity.owner(scope, self._secret) for scope in scopes)
        with self._transaction() as cursor:
            if arguments.action in ("save", "forget"):
                result = self._write(cursor, owners[0], scopes[0], arguments)
            elif arguments.action == "tags":
                result = self._tags(cursor, owners, arguments.offset)
            else:
                result = self._recall(cursor, owners, arguments.tags)
            self._check_deadline(deadline_at)
            return result

    def _check_deadline(self, deadline_at: float | None) -> None:
        """过期操作在事务提交前回滚。"""
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise TaggedMemoryStoreError("本次记忆请求已超时")

    def _write(
        self,
        cursor: Any,
        owner: str,
        scope: MemoryScope,
        arguments: MemoryArguments,
    ) -> MemoryResult:
        """锁定作用域后只新增事实或标记遗忘，已有内容和标签不可编辑。"""
        cursor.execute(
            "INSERT INTO chat_memory_scope (scope_id, scope) VALUES (%s, %s) "
            "ON DUPLICATE KEY UPDATE scope = VALUES(scope)",
            (owner, scope),
        )
        # upsert 持有作用域行的排他锁，避免并发 INSERT IGNORE 后共享锁升级相互等待。
        cursor.execute(
            "SELECT id, is_deleted FROM chat_tagged_memory "
            "WHERE scope_id = %s AND memory_key = %s",
            (owner, arguments.key),
        )
        existing = cursor.fetchone()
        now = datetime.now()
        if arguments.action == "forget":
            if not existing or existing["is_deleted"]:
                return MemoryResult(status="not_found")
            cursor.execute(
                "UPDATE chat_tagged_memory "
                "SET is_deleted = 1, update_time = %s WHERE id = %s",
                (now, existing["id"]),
            )
            return MemoryResult(status="forgotten")
        if existing:
            raise TaggedMemoryConflictError("记忆键已存在，不能编辑或恢复，请使用新键新增")
        cursor.execute(
            "SELECT COUNT(*) AS count FROM chat_tagged_memory "
            "WHERE scope_id = %s AND is_deleted = 0", (owner,),
        )
        if cursor.fetchone()["count"] >= 256:
            raise TaggedMemoryCapacityError("此作用域记忆已满，请明确删除旧记忆后使用新键新增")
        cursor.execute(
            "INSERT INTO chat_tagged_memory "
            "(scope_id, memory_key, content, create_time, update_time) "
            "VALUES (%s, %s, %s, %s, %s)",
            (owner, arguments.key, arguments.content, now, now),
        )
        memory_id = cursor.lastrowid
        cursor.executemany(
            "INSERT INTO chat_memory_tag (memory_id, tag) VALUES (%s, %s)",
            [(memory_id, tag) for tag in arguments.tags],
        )
        return MemoryResult(status="saved")

    def _admin_views(self, cursor: Any, rows: list[dict]) -> tuple[MemoryView, ...]:
        """管理员批量读取原文及匿名归属，不推测用户或群聊身份。"""
        if not rows:
            return ()
        ids = tuple(row["id"] for row in rows)
        cursor.execute("SELECT memory_id,tag FROM chat_memory_tag WHERE memory_id IN ("
                       + ",".join("%s" for _ in ids) + ") ORDER BY tag", ids)
        tags: dict[int, list[str]] = {id: [] for id in ids}
        for row in cursor.fetchall():
            tags[row["memory_id"]].append(row["tag"])
        return tuple(MemoryView(id=row["id"], scope_id=row["scope_id"], scope=row["scope"],
                                key=row["memory_key"], content=row["content"],
                                tags=tuple(tags[row["id"]]), created_at=row["create_time"])
                     for row in rows)

    def _admin_get(self, cursor: Any, id: int) -> MemoryView:
        """读取和删除共同检查有效记录，不将已删除原文重新开放。"""
        cursor.execute("SELECT m.*,s.scope FROM chat_tagged_memory m JOIN chat_memory_scope s "
                       "ON s.scope_id=m.scope_id WHERE m.id=%s AND m.is_deleted=0", (id,))
        row = cursor.fetchone()
        if row is None:
            raise KnowledgeError("MEMORY_NOT_FOUND", status_code=404)
        return self._admin_views(cursor, [row])[0]

    def admin_create(self, body: MemoryCreate) -> MemoryView:
        """管理员只新增到明确的已有作用域或当前 HMAC 命名空间。"""
        with self._transaction() as cursor:
            if body.scope_id:
                # MySQL 普通读会建立 RR 快照；必须先锁作用域，容量和同键检查才看到并发提交。
                cursor.execute("SELECT scope FROM chat_memory_scope WHERE scope_id=%s FOR UPDATE",
                               (body.scope_id,))
                row = cursor.fetchone()
                if row is None or row["scope"] != body.scope:
                    raise KnowledgeError("MEMORY_SCOPE_NOT_FOUND", status_code=404)
                owner = body.scope_id
            else:
                owner = memory_scope_owner(body.bot_id, body.scope, body.target or "", self._secret)
            arguments = MemoryArguments(action="save", scope=body.scope, key=body.key,
                                        content=body.content, tags=body.tags)
            self._write(cursor, owner, body.scope, arguments)
            cursor.execute("SELECT id FROM chat_tagged_memory WHERE scope_id=%s AND memory_key=%s",
                           (owner, body.key))
            return self._admin_get(cursor, cursor.fetchone()["id"])

    def admin_get(self, id: int) -> MemoryView:
        """仅由已认证管理入口读取完整有效记忆。"""
        with self._transaction() as cursor:
            return self._admin_get(cursor, id)

    def _admin_conditions(self, *, query: str = "", tag: str = "",
                          scope: str | None = None, scope_id: str | None = None
                          ) -> tuple[list[str], list[Any]]:
        """固定列名组成筛选，关键词按字面量匹配，所有用户值参数化。"""
        conditions, args = ["m.is_deleted=0"], []
        if query:
            conditions.append("(m.memory_key LIKE %s ESCAPE '!' OR m.content LIKE %s ESCAPE '!')")
            args.extend((literal_like(query), literal_like(query)))
        if tag:
            conditions.append("EXISTS (SELECT 1 FROM chat_memory_tag t "
                              "WHERE t.memory_id=m.id AND t.tag=%s)")
            args.append(tag)
        for name, value in (("s.scope", scope), ("m.scope_id", scope_id)):
            if value:
                conditions.append(name + "=%s")
                args.append(value)
        return conditions, args

    def admin_list(self, *, limit: int = 20, cursor: str | None = None, query: str = "",
                   tag: str = "", scope: str | None = None, scope_id: str | None = None
                   ) -> Page[MemoryView]:
        """管理员列表使用筛选绑定游标，QQ 召回仍只经过绑定身份。"""
        page_limit(limit)
        filters = {"query": query, "tag": tag, "scope": scope or "", "scope_id": scope_id or ""}
        last_id = decode_cursor(cursor, filters)
        conditions, args = self._admin_conditions(
            query=query, tag=tag, scope=scope, scope_id=scope_id
        )
        if last_id is not None:
            conditions.append("m.id<%s")
            args.append(last_id)
        with self._transaction() as db_cursor:
            db_cursor.execute("SELECT m.*,s.scope FROM chat_tagged_memory m "
                              "JOIN chat_memory_scope s ON s.scope_id=m.scope_id WHERE "
                              + " AND ".join(conditions) + " ORDER BY m.id DESC LIMIT %s",
                              (*args, limit + 1))
            rows = db_cursor.fetchall()
            items = self._admin_views(db_cursor, rows[:limit])
            return Page(items=items, next_cursor=encode_cursor(items[-1].id, filters)
                        if len(rows) > limit else None)

    def admin_tags(self, *, scope: str | None = None,
                   scope_id: str | None = None) -> tuple[TagCount, ...]:
        """只统计当前作用域筛选下仍可读取的记忆标签。"""
        conditions, args = self._admin_conditions(scope=scope, scope_id=scope_id)
        with self._transaction() as cursor:
            cursor.execute("SELECT t.tag,COUNT(*) AS count FROM chat_memory_tag t "
                           "JOIN chat_tagged_memory m ON m.id=t.memory_id "
                           "JOIN chat_memory_scope s ON s.scope_id=m.scope_id WHERE "
                           + " AND ".join(conditions) + " GROUP BY t.tag ORDER BY count DESC,t.tag",
                           tuple(args))
            return tuple(TagCount.model_validate(row) for row in cursor.fetchall())

    def admin_scopes(self, *, scope: str | None = None) -> tuple[ScopeView, ...]:
        """保留空作用域以便新增另一键，不公开任何可逆 QQ 身份。"""
        with self._transaction() as cursor:
            cursor.execute("SELECT s.scope_id,s.scope,COUNT(m.id) AS count "
                           "FROM chat_memory_scope s "
                           "LEFT JOIN chat_tagged_memory m ON m.scope_id=s.scope_id "
                           "AND m.is_deleted=0" + (" WHERE s.scope=%s" if scope else "")
                           + " GROUP BY s.scope_id,s.scope ORDER BY s.scope,s.scope_id",
                           (scope,) if scope else ())
            return tuple(ScopeView.model_validate(row) for row in cursor.fetchall())

    def admin_delete(self, id: int) -> None:
        """管理员明确删除只变更状态，记录正文、标签和原 ID 保留。"""
        with self._transaction() as cursor:
            self._admin_get(cursor, id)
            cursor.execute("UPDATE chat_tagged_memory SET is_deleted=1,update_time=%s "
                           "WHERE id=%s AND is_deleted=0", (datetime.now(), id))
            if cursor.rowcount == 0:
                raise KnowledgeError("MEMORY_NOT_FOUND", status_code=404)

    def _tags(self, cursor: Any, owners: tuple[str, ...], offset: int) -> MemoryResult:
        """分页只提供可见标签，不在工具声明中提前泄露记忆正文。"""
        placeholders = ",".join("%s" for _ in owners)
        cursor.execute(
            "SELECT s.scope, t.tag FROM chat_memory_tag t "
            "JOIN chat_tagged_memory m ON m.id = t.memory_id "
            "JOIN chat_memory_scope s ON s.scope_id = m.scope_id "
            f"WHERE m.scope_id IN ({placeholders}) AND m.is_deleted = 0 "
            "GROUP BY s.scope, t.tag "
            "ORDER BY MAX(m.update_time) DESC, s.scope, t.tag LIMIT 65 OFFSET %s",
            (*owners, offset),
        )
        rows = cursor.fetchall()
        return MemoryResult(
            status="tags",
            tags=tuple(MemoryTag.model_validate(row) for row in rows[:64]),
            next_offset=offset + 64 if len(rows) > 64 else None,
        )

    def _recall(
        self,
        cursor: Any,
        owners: tuple[str, ...],
        tags: tuple[str, ...],
    ) -> MemoryResult:
        """按标签交集数量排序，每次最多加载八条完整事实。"""
        owner_parameters = ",".join("%s" for _ in owners)
        tag_parameters = ",".join("%s" for _ in tags)
        cursor.execute(
            "SELECT m.id, s.scope, m.memory_key, m.content "
            "FROM chat_tagged_memory m "
            "JOIN chat_memory_scope s ON s.scope_id = m.scope_id "
            "JOIN chat_memory_tag t ON t.memory_id = m.id "
            f"WHERE m.scope_id IN ({owner_parameters}) AND m.is_deleted = 0 "
            f"AND t.tag IN ({tag_parameters}) "
            "GROUP BY m.id, s.scope, m.memory_key, m.content, m.update_time "
            "ORDER BY COUNT(*) DESC, m.update_time DESC, m.id LIMIT 8",
            (*owners, *tags),
        )
        rows = cursor.fetchall()
        if not rows:
            return MemoryResult(status="recalled")
        memory_ids = tuple(row["id"] for row in rows)
        memory_parameters = ",".join("%s" for _ in memory_ids)
        cursor.execute(
            "SELECT memory_id, tag FROM chat_memory_tag "
            f"WHERE memory_id IN ({memory_parameters}) ORDER BY tag",
            memory_ids,
        )
        memory_tags = cursor.fetchall()
        records: list[MemoryRecord] = []
        for row in rows:
            records.append(
                MemoryRecord(
                    scope=row["scope"],
                    key=row["memory_key"],
                    content=row["content"],
                    tags=tuple(
                        item["tag"] for item in memory_tags if item["memory_id"] == row["id"]
                    ),
                )
            )
        return MemoryResult(status="recalled", records=tuple(records))
