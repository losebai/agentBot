"""MySQL 或 SQLite 中标签记忆、授权作用域及关联的事务边界。"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import Counter
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
from agent_bot.knowledge.memory_titles import scope_titles
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
    """CREATE TABLE IF NOT EXISTS chat_tagged_memory (
        id INT NOT NULL AUTO_INCREMENT COMMENT '记忆自增主键',
        scope_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '关联作用域',
        memory_key VARCHAR(80) COLLATE utf8mb4_bin NOT NULL COMMENT '稳定事实键',
        content VARCHAR(1000) NOT NULL COMMENT '已确认的事实',
        is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '逻辑删除标记',
        create_time DATETIME NOT NULL COMMENT '创建时间',
        update_time DATETIME NOT NULL COMMENT '更新时间',
        scope VARCHAR(16) NOT NULL COMMENT 'global/session/user/group',
        tags_json JSON NOT NULL COMMENT '规范化主题标签数组',
        material_ids_json JSON NOT NULL COMMENT '引用素材标识数组',
        PRIMARY KEY (id),
        UNIQUE KEY uk_tagged_memory_scope_key (scope_id, memory_key),
        KEY idx_tagged_memory_scope_time (scope_id, is_deleted, update_time)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LLM 自主保存的标签记忆'""",
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
        # 管理列表与作用域目录常同时读取，相同目录快照只匹配新增目标；不缓存陈旧名称。
        self._scope_title_lock = threading.Lock()
        self._scope_title_profiles: list[dict[str, Any]] | None = None
        self._scope_title_cache: dict[tuple[str, MemoryScope], str | None] = {}

    @contextmanager
    def _transaction(self, owner: str | None = None) -> Iterator[Any]:
        """同作用域写入先取得命名锁，提交或回滚后释放，避免容量和墓碑竞争。"""
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
                with connection.cursor() as cursor:
                    acquired = False
                    lock_name = hashlib.sha256(
                        f"agent_bot:memory:{self._params.database}:{owner}".encode()
                    ).hexdigest()
                    try:
                        if owner is not None and self._params.backend != "sqlite":
                            # GET_LOCK 不建立一致性快照；等待结束后首次业务读可见前一写入。
                            cursor.execute("SELECT GET_LOCK(%s,10) AS acquired", (lock_name,))
                            if cursor.fetchone()["acquired"] != 1:
                                raise TaggedMemoryStoreError("记忆作用域正在写入，请稍后重试")
                            acquired = True
                        yield cursor
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    finally:
                        if acquired:
                            cursor.execute("SELECT RELEASE_LOCK(%s) AS released", (lock_name,))
                            if cursor.fetchone()["released"] != 1:
                                raise TaggedMemoryStoreError("记忆作用域写锁释放失败")
        except pymysql.MySQLError as error:
            raise TaggedMemoryStoreError("记忆数据库暂时不可用") from error

    def initialize(self) -> None:
        """显式创建单张事实表；已有部署的字段升级由独立迁移完成。"""
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
        cursor.execute("SELECT id,scope_id,memory_key,content,is_deleted,create_time,"
                       "update_time,scope,tags_json,material_ids_json "
                       "FROM chat_tagged_memory LIMIT 0")

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
        writing = arguments.action in ("save", "forget")
        with self._transaction(owners[0] if writing else None) as cursor:
            if writing:
                if arguments.material_ids:
                    self._validate_materials(cursor, identity.session_id, arguments.material_ids)
                result = self._write(cursor, owners[0], scopes[0], arguments)
            elif arguments.action == "tags":
                result = self._tags(cursor, owners, arguments.offset)
            else:
                result = self._recall(cursor, owners, arguments.tags, identity.session_id)
            self._check_deadline(deadline_at)
            return result

    @staticmethod
    def _validate_materials(cursor: Any, session_id: str, ids: tuple[str, ...]) -> None:
        """素材校验与记忆保存共用事务，不能用共享记忆扩大原件访问权限。"""
        cursor.execute("SELECT id FROM chat_material WHERE entry='qq' AND session_id=%s "
                       "AND id IN (" + ",".join("%s" for _ in ids) + ")", (session_id, *ids))
        if {row["id"] for row in cursor.fetchall()} != set(ids):
            raise ValueError("素材不存在或不属于当前会话")

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
        # 事务入口已串行化同作用域写入；唯一约束继续保护含已删除墓碑的事实键。
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
            "(scope_id, memory_key, content, create_time, update_time, scope, "
            "tags_json, material_ids_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (owner, arguments.key, arguments.content, now, now, scope,
             json.dumps(sorted(arguments.tags), ensure_ascii=False),
             json.dumps(sorted(arguments.material_ids))),
        )
        return MemoryResult(status="saved")

    def _admin_views(self, cursor: Any, rows: list[dict]) -> tuple[MemoryView, ...]:
        """管理员批量读取原文及匿名归属，不推测用户或群聊身份。"""
        return tuple(MemoryView(id=row["id"], scope_id=row["scope_id"], scope=row["scope"],
                                key=row["memory_key"], content=row["content"],
                                tags=tuple(json.loads(row["tags_json"])),
                                created_at=row["create_time"],
                                material_ids=tuple(json.loads(row["material_ids_json"])))
                     for row in rows)

    def _admin_get(self, cursor: Any, id: int) -> MemoryView:
        """读取和删除共同检查有效记录，不将已删除原文重新开放。"""
        cursor.execute("SELECT m.* FROM chat_tagged_memory m WHERE m.id=%s AND m.is_deleted=0",
                       (id,))
        row = cursor.fetchone()
        if row is None:
            raise KnowledgeError("MEMORY_NOT_FOUND", status_code=404)
        return self._admin_views(cursor, [row])[0]

    def admin_create(self, body: MemoryCreate) -> MemoryView:
        """管理员只新增到明确的已有作用域或当前 HMAC 命名空间。"""
        owner = body.scope_id or memory_scope_owner(
            body.bot_id, body.scope, body.target or "", self._secret
        )
        with self._transaction(owner) as cursor:
            if body.scope_id:
                # 已删除事实保留授权类型；无任何事实的未知 scope_id 不能凭管理员参数创建。
                cursor.execute("SELECT scope FROM chat_tagged_memory WHERE scope_id=%s LIMIT 1",
                               (body.scope_id,))
                row = cursor.fetchone()
                if row is None or row["scope"] != body.scope:
                    raise KnowledgeError("MEMORY_SCOPE_NOT_FOUND", status_code=404)
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
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each(m.tags_json) WHERE value=%s)"
                if self._params.backend == "sqlite"
                else "JSON_CONTAINS(m.tags_json,JSON_QUOTE(%s))"
            )
            args.append(tag)
        for name, value in (("m.scope", scope), ("m.scope_id", scope_id)):
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
            db_cursor.execute("SELECT m.* FROM chat_tagged_memory m WHERE "
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
            cursor.execute("SELECT m.tags_json FROM chat_tagged_memory m WHERE "
                           + " AND ".join(conditions),
                           tuple(args))
            counts = Counter(tag for row in cursor.fetchall()
                             for tag in json.loads(row["tags_json"]))
            return tuple(TagCount(tag=tag, count=count)
                         for tag, count in sorted(counts.items(),
                                                  key=lambda item: (-item[1], item[0])))

    def admin_scopes(self, *, scope: str | None = None) -> tuple[ScopeView, ...]:
        """从全部事实含墓碑列出授权类型，删除全部事实后仍允许新增另一键。"""
        with self._transaction() as cursor:
            cursor.execute("SELECT scope_id,scope,SUM(CASE WHEN is_deleted=0 THEN 1 ELSE 0 END) "
                           "AS count FROM chat_tagged_memory"
                           + (" WHERE scope=%s" if scope else "")
                           + " GROUP BY scope_id,scope ORDER BY scope,scope_id",
                           (scope,) if scope else ())
            return tuple(ScopeView.model_validate(row) for row in cursor.fetchall())

    def admin_scope_titles(self, owners: dict[str, MemoryScope]) -> dict[str, str]:
        """归还数据库事务后匹配；逐字段核对目录快照，昵称变化立即使缓存失效。"""
        if not owners:
            return {}
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT bot_id,group_id,user_id,group_name,nickname,card FROM chat_group_member "
                "ORDER BY update_time DESC,bot_id,group_id,user_id",
            )
            profiles = cursor.fetchall()
        with self._scope_title_lock:
            if profiles != self._scope_title_profiles:
                self._scope_title_profiles = profiles
                self._scope_title_cache.clear()
            missing = {key: scope for key, scope in owners.items()
                       if (key, scope) not in self._scope_title_cache}
            if missing:
                resolved = scope_titles(missing, profiles, self._secret)
                self._scope_title_cache.update(
                    ((key, scope), resolved.get(key)) for key, scope in missing.items()
                )
            return {key: title for key, scope in owners.items()
                    if (title := self._scope_title_cache[(key, scope)]) is not None}

    def admin_delete(self, id: int) -> None:
        """管理员明确删除只变更状态，记录正文、标签和原 ID 保留。"""
        with self._transaction() as cursor:
            self._admin_get(cursor, id)
            cursor.execute("UPDATE chat_tagged_memory SET is_deleted=1,update_time=%s "
                           "WHERE id=%s AND is_deleted=0", (datetime.now(), id))
            if cursor.rowcount == 0:
                raise KnowledgeError("MEMORY_NOT_FOUND", status_code=404)

    def _tags(self, cursor: Any, owners: tuple[str, ...], offset: int) -> MemoryResult:
        """最多四个受权作用域各 256 条事实，聚合后按最近使用稳定分页标签。"""
        placeholders = ",".join("%s" for _ in owners)
        cursor.execute(
            "SELECT scope,tags_json,update_time FROM chat_tagged_memory "
            f"WHERE scope_id IN ({placeholders}) AND is_deleted=0",
            owners,
        )
        recent: dict[tuple[str, str], datetime] = {}
        for row in cursor.fetchall():
            for tag in json.loads(row["tags_json"]):
                key = (row["scope"], tag)
                recent[key] = max(recent.get(key, row["update_time"]), row["update_time"])
        keys = sorted(recent)
        keys.sort(key=recent.__getitem__, reverse=True)
        page = keys[offset:offset + 65]
        return MemoryResult(
            status="tags",
            tags=tuple(MemoryTag(scope=scope, tag=tag) for scope, tag in page[:64]),
            next_offset=offset + 64 if len(page) > 64 else None,
        )

    def _recall(
        self,
        cursor: Any,
        owners: tuple[str, ...],
        tags: tuple[str, ...],
        session_id: str,
    ) -> MemoryResult:
        """按标签交集数量排序，每次最多加载八条完整事实。"""
        owner_parameters = ",".join("%s" for _ in owners)
        cursor.execute(
            "SELECT id,scope,memory_key,content,tags_json,material_ids_json,update_time "
            "FROM chat_tagged_memory "
            f"WHERE scope_id IN ({owner_parameters}) AND is_deleted=0",
            owners,
        )
        requested = set(tags)
        rows = []
        for row in cursor.fetchall():
            row["tags"] = tuple(json.loads(row["tags_json"]))
            row["matches"] = len(requested.intersection(row["tags"]))
            if row["matches"]:
                rows.append(row)
        rows.sort(key=lambda row: row["id"])
        rows.sort(key=lambda row: (row["matches"], row["update_time"]), reverse=True)
        rows = rows[:8]
        if not rows:
            return MemoryResult(status="recalled")
        material_ids = tuple(sorted({item for row in rows
                                     for item in json.loads(row["material_ids_json"])}))
        visible_materials: set[str] = set()
        if material_ids:
            # 共享事实只共享文字；素材原件仍要求来源会话，不能直接信任记忆内的 ID。
            cursor.execute("SELECT id FROM chat_material WHERE entry='qq' AND session_id=%s "
                           "AND id IN (" + ",".join("%s" for _ in material_ids) + ")",
                           (session_id, *material_ids))
            visible_materials = {row["id"] for row in cursor.fetchall()}
        records: list[MemoryRecord] = []
        for row in rows:
            records.append(
                MemoryRecord(
                    scope=row["scope"],
                    key=row["memory_key"],
                    content=row["content"],
                    material_ids=tuple(item for item in json.loads(row["material_ids_json"])
                                       if item in visible_materials),
                    tags=row["tags"],
                )
            )
        return MemoryResult(status="recalled", records=tuple(records))
