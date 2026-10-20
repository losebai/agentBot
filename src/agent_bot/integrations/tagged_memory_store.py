"""MySQL 或 SQLite 中标签记忆、授权作用域及关联的事务边界。"""

from __future__ import annotations

import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from typing import Any, cast

import pymysql

from agent_bot.database import database_connector
from agent_bot.knowledge.contracts import MemoryView
from agent_bot.knowledge.memory_titles import scope_titles
from agent_bot.memory.schema import MEMORY_COLUMNS, MYSQL_ADDITIONAL
from agent_bot.services.tagged_memory import (
    MemoryArguments,
    MemoryIdentity,
    MemoryResult,
    MemoryScope,
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
SCHEMA_STATEMENTS = (
    SCHEMA_STATEMENTS[0].replace(
        "        PRIMARY KEY (id),",
        "".join(
            f"        {name} {kind} COMMENT '{comment}',\n"
            for name, (kind, comment) in MEMORY_COLUMNS.items()
        )
        + "        PRIMARY KEY (id),",
    ),
    *MYSQL_ADDITIONAL,
)
SCHEMA_STATEMENTS = (
    SCHEMA_STATEMENTS[0].replace(
        "KEY idx_tagged_memory_scope_time (scope_id, is_deleted, update_time)",
        "KEY idx_tagged_memory_scope_time (scope_id, is_deleted, update_time),\n"
        "        KEY idx_memory_status (scope_id,status),\n"
        "        KEY idx_memory_fingerprint (scope_id,subject_id,content_fingerprint),\n"
        "        KEY idx_memory_topic (scope_id,subject_id,topic),\n"
        "        KEY idx_memory_merged (merged_into_id)",
    ),
    *SCHEMA_STATEMENTS[1:],
)


class TaggedMemoryStoreError(RuntimeError):
    """可对外返回的固定数据库边界错误。"""


class TaggedMemoryConflictError(TaggedMemoryStoreError):
    """重复、版本或墓碑冲突必须明确修订或核对，不能换键绕过。"""


class TaggedMemoryCapacityError(TaggedMemoryStoreError):
    """作用域当前名额已满，需明确失效、合并或遗忘后再新增。"""


from agent_bot.integrations.memory_lifecycle import MemoryLifecycleMixin  # noqa: E402


class TaggedMemoryStore(MemoryLifecycleMixin):
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
                        if owner is not None and self._params.backend == "sqlite":
                            connection.begin()
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
        cursor.execute(
            "SELECT id,scope_id,memory_key,content,is_deleted,create_time,"
            "update_time,scope,tags_json,material_ids_json "
            "FROM chat_tagged_memory LIMIT 0"
        )
        cursor.execute("SELECT " + ",".join(MEMORY_COLUMNS) + " FROM chat_tagged_memory LIMIT 0")
        for table in (
            "chat_memory_revision",
            "chat_memory_evidence",
            "chat_memory_operation",
            "chat_memory_embedding",
        ):
            cursor.execute(f"SELECT * FROM {table} LIMIT 0")

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
        writing = arguments.action not in ("recall", "tags")
        with self._transaction(owners[0] if writing else None) as cursor:
            if writing:
                if arguments.material_ids:
                    self._validate_materials(cursor, identity.session_id, arguments.material_ids)
                replay = self._operation_result(cursor, owners[0], identity, arguments)
                if replay is not None:
                    return replay
                result = self._write(cursor, owners[0], scopes[0], arguments, identity=identity)
                self._remember_operation(cursor, owners[0], identity, arguments, result)
            elif arguments.action == "tags":
                result = self._tags(cursor, owners, arguments.offset)
            else:
                result = self._recall(
                    cursor, owners, arguments.tags, identity.session_id, identity=identity
                )
            self._check_deadline(deadline_at)
            return result

    @staticmethod
    def _validate_materials(cursor: Any, session_id: str, ids: tuple[str, ...]) -> None:
        """素材校验与记忆保存共用事务，不能用共享记忆扩大原件访问权限。"""
        cursor.execute(
            "SELECT id FROM chat_material WHERE entry='qq' AND session_id=%s "
            "AND id IN (" + ",".join("%s" for _ in ids) + ")",
            (session_id, *ids),
        )
        if {row["id"] for row in cursor.fetchall()} != set(ids):
            raise ValueError("素材不存在或不属于当前会话")

    def _check_deadline(self, deadline_at: float | None) -> None:
        """过期操作在事务提交前回滚。"""
        if deadline_at is not None and time.monotonic() >= deadline_at:
            raise TaggedMemoryStoreError("本次记忆请求已超时")

    def admin_get(self, id: int) -> MemoryView:
        """仅由已认证管理入口读取完整有效记忆。"""
        with self._transaction() as cursor:
            return self._admin_get(cursor, id)

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
            missing = {
                key: scope
                for key, scope in owners.items()
                if (key, scope) not in self._scope_title_cache
            }
            if missing:
                resolved = scope_titles(missing, profiles, self._secret)
                self._scope_title_cache.update(
                    ((key, scope), resolved.get(key)) for key, scope in missing.items()
                )
            return {
                key: title
                for key, scope in owners.items()
                if (title := self._scope_title_cache[(key, scope)]) is not None
            }
