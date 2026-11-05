"""MySQL 工作流目录存储边界。"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, replace
from typing import Any

import pymysql
from pymysql.err import DatabaseError, InterfaceError, OperationalError

from agent_bot.database import database_connector
from agent_bot.services.workflows import (
    WorkflowDefinition,
    WorkflowMetadataPatch,
    validate_workflow_bindings,
)
from agent_bot.settings import DatabaseParams

DEFAULT_WORKFLOW_TABLE = "chat_workflow"
WORKFLOW_CONTENT_HASH_INDEX = "uk_chat_workflow_content_sha256"
WORKFLOW_COLUMNS = frozenset(
    {
        "id",
        "workflow_key",
        "display_name",
        "source_file",
        "source_format",
        "ui_json",
        "api_json",
        "positive_node_id",
        "negative_node_id",
        "output_node_id",
        "seed_node_id",
        "description",
        "additional_positive_prompt",
        "additional_negative_prompt",
        "enabled",
        "content_sha256",
        "create_time",
        "update_time",
        "workflow_type",
        "input_image_node_id",
        "positive_input_name",
        "negative_input_name",
    }
)
_SELECT_COLUMNS = (
    "workflow_key, display_name, source_file, source_format, ui_json, api_json, "
    "positive_node_id, negative_node_id, output_node_id, seed_node_id, enabled, "
    "content_sha256, description, additional_positive_prompt, "
    "additional_negative_prompt, workflow_type, input_image_node_id, "
    "positive_input_name, negative_input_name"
)
_METADATA_COLUMN_DEFINITIONS = (
    ("workflow_type", "VARCHAR(16) NOT NULL DEFAULT 'text_to_image' COMMENT '工作流类型'"),
    ("input_image_node_id", "VARCHAR(64) NULL COMMENT '用户原图 LoadImage 节点 ID'"),
    ("positive_input_name", "VARCHAR(64) NOT NULL DEFAULT 'text' COMMENT '正向或编辑指令字段名'"),
    ("negative_input_name", "VARCHAR(64) NOT NULL DEFAULT 'text' COMMENT '负向提示词字段名'"),
    (
        "description",
        "VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '供 Agent 选择工作流的用途描述'",
    ),
    (
        "additional_positive_prompt",
        "VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '生成时固定追加的正向提示词'",
    ),
    (
        "additional_negative_prompt",
        "VARCHAR(2000) NOT NULL DEFAULT '' COMMENT '生成时固定追加的负向提示词'",
    ),
)


@dataclass(frozen=True, slots=True)
class _WorkflowIdentity:
    """数据库中用于解析 key、来源文件与内容哈希冲突的最小身份。"""

    id: int
    workflow_key: str
    source_file: str
    content_sha256: str


class WorkflowStoreError(RuntimeError):
    """工作流数据库边界的稳定错误。"""


class WorkflowNotFoundError(WorkflowStoreError):
    """请求的工作流不存在，或执行入口要求的工作流已禁用。"""


class WorkflowRevisionConflictError(WorkflowStoreError):
    """工作流在管理窗口读取后被编辑或重新同步。"""


def workflow_revision(definition: WorkflowDefinition) -> str:
    """对完整记录取摘要，技术同步和业务编辑均使旧窗口版本失效。"""
    content = json.dumps(asdict(definition), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


class WorkflowStore:
    """以增量迁移和 upsert 管理工作流目录。"""

    def __init__(
        self,
        params: DatabaseParams,
        *,
        table: str = DEFAULT_WORKFLOW_TABLE,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        """保存连接参数和可替换的数据库连接工厂。"""

        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", table):
            raise ValueError("workflow table name is invalid")
        self._params = params
        self._table = table
        self._connect = connect or database_connector(params)

    def prepare_for_startup(self) -> None:
        """校验已部署基础表，仅增补兼容元数据列，不重写记录或收窄已有字段。"""

        metadata_columns = {name for name, _ in _METADATA_COLUMN_DEFINITIONS}
        try:
            with closing(self._open()) as connection:
                columns = self._read_columns(connection)
                if (
                    not (WORKFLOW_COLUMNS - metadata_columns).issubset(columns)
                    or not self._content_hash_index_exists(connection)
                ):
                    raise WorkflowStoreError("workflow database schema is invalid")
                self._add_missing_metadata_columns(connection)
                self._validate_schema(connection)
                connection.commit()
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise WorkflowStoreError("workflow database schema preparation failed") from exc

    def migrate(self) -> None:
        """补齐表结构，合并历史重复内容后建立内容哈希唯一索引。"""

        if self._params.backend == "sqlite":
            # SQLite 新库结构由 database init 提供，迁移命令不运行 MySQL 重写逻辑。
            self.prepare_for_startup()
            return

        try:
            with closing(self._open()) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        CREATE TABLE IF NOT EXISTS `{self._table}` (
                            id BIGINT NOT NULL AUTO_INCREMENT COMMENT '主键',
                            workflow_key VARCHAR(128) NOT NULL COMMENT '稳定工作流键',
                            display_name VARCHAR(256) NOT NULL COMMENT '工作流显示名称',
                            source_file VARCHAR(512) NOT NULL COMMENT '导入来源文件名',
                            source_format VARCHAR(16) NOT NULL COMMENT '来源格式：ui 或 api',
                            ui_json MEDIUMTEXT NULL COMMENT 'ComfyUI 画布工作流 JSON',
                            api_json MEDIUMTEXT NULL
                                COMMENT '可提交给 ComfyUI 的 API 工作流 JSON',
                            positive_node_id VARCHAR(64) NULL COMMENT '正向提示词节点 ID',
                            negative_node_id VARCHAR(64) NULL COMMENT '负向提示词节点 ID',
                            output_node_id VARCHAR(64) NULL COMMENT '图片输出节点 ID',
                            seed_node_id VARCHAR(64) NULL COMMENT '随机种子所在节点 ID',
                            workflow_type VARCHAR(16) NOT NULL DEFAULT 'text_to_image'
                                COMMENT '工作流类型',
                            input_image_node_id VARCHAR(64) NULL
                                COMMENT '用户原图 LoadImage 节点 ID',
                            positive_input_name VARCHAR(64) NOT NULL DEFAULT 'text'
                                COMMENT '正向或编辑指令字段名',
                            negative_input_name VARCHAR(64) NOT NULL DEFAULT 'text'
                                COMMENT '负向提示词字段名',
                            description VARCHAR(2000) NOT NULL DEFAULT ''
                                COMMENT '供 Agent 选择工作流的用途描述',
                            additional_positive_prompt VARCHAR(2000) NOT NULL DEFAULT ''
                                COMMENT '生成时固定追加的正向提示词',
                            additional_negative_prompt VARCHAR(2000) NOT NULL DEFAULT ''
                                COMMENT '生成时固定追加的负向提示词',
                            enabled TINYINT(1) NOT NULL DEFAULT 1
                                COMMENT '是否允许 Agent 执行',
                            content_sha256 CHAR(64) NOT NULL
                                COMMENT '规范化来源内容 SHA-256',
                            create_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                COMMENT '创建时间',
                            update_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                            PRIMARY KEY (id),
                            UNIQUE KEY uk_chat_workflow_key (workflow_key),
                            UNIQUE KEY uk_chat_workflow_content_sha256
                                (content_sha256),
                            KEY idx_chat_workflow_enabled (enabled)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                            COMMENT='本地 Comfy 工作流定义与执行绑定'
                        """
                    )
                self._add_missing_metadata_columns(connection)
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"""
                        ALTER TABLE `{self._table}`
                            MODIFY COLUMN id BIGINT NOT NULL AUTO_INCREMENT
                                COMMENT '主键',
                            MODIFY COLUMN workflow_key VARCHAR(128) NOT NULL
                                COMMENT '稳定工作流键',
                            MODIFY COLUMN display_name VARCHAR(256) NOT NULL
                                COMMENT '工作流显示名称',
                            MODIFY COLUMN source_file VARCHAR(512) NOT NULL
                                COMMENT '导入来源文件名',
                            MODIFY COLUMN source_format VARCHAR(16) NOT NULL
                                COMMENT '来源格式：ui 或 api',
                            MODIFY COLUMN ui_json MEDIUMTEXT NULL
                                COMMENT 'ComfyUI 画布工作流 JSON',
                            MODIFY COLUMN api_json MEDIUMTEXT NULL
                                COMMENT '可提交给 ComfyUI 的 API 工作流 JSON',
                            MODIFY COLUMN positive_node_id VARCHAR(64) NULL
                                COMMENT '正向提示词节点 ID',
                            MODIFY COLUMN negative_node_id VARCHAR(64) NULL
                                COMMENT '负向提示词节点 ID',
                            MODIFY COLUMN output_node_id VARCHAR(64) NULL
                                COMMENT '图片输出节点 ID',
                            MODIFY COLUMN seed_node_id VARCHAR(64) NULL
                                COMMENT '随机种子所在节点 ID',
                            MODIFY COLUMN workflow_type VARCHAR(16) NOT NULL
                                DEFAULT 'text_to_image' COMMENT '工作流类型',
                            MODIFY COLUMN input_image_node_id VARCHAR(64) NULL
                                COMMENT '用户原图 LoadImage 节点 ID',
                            MODIFY COLUMN positive_input_name VARCHAR(64) NOT NULL
                                DEFAULT 'text' COMMENT '正向或编辑指令字段名',
                            MODIFY COLUMN negative_input_name VARCHAR(64) NOT NULL
                                DEFAULT 'text' COMMENT '负向提示词字段名',
                            MODIFY COLUMN description VARCHAR(2000) NOT NULL DEFAULT ''
                                COMMENT '供 Agent 选择工作流的用途描述',
                            MODIFY COLUMN additional_positive_prompt VARCHAR(2000)
                                NOT NULL DEFAULT ''
                                COMMENT '生成时固定追加的正向提示词',
                            MODIFY COLUMN additional_negative_prompt VARCHAR(2000)
                                NOT NULL DEFAULT ''
                                COMMENT '生成时固定追加的负向提示词',
                            MODIFY COLUMN enabled TINYINT(1) NOT NULL DEFAULT 1
                                COMMENT '是否允许 Agent 执行',
                            MODIFY COLUMN content_sha256 CHAR(64) NOT NULL
                                COMMENT '规范化来源内容 SHA-256',
                            MODIFY COLUMN create_time DATETIME NOT NULL
                                DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                            MODIFY COLUMN update_time DATETIME NOT NULL
                                DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                                COMMENT '更新时间',
                            COMMENT='本地 Comfy 工作流定义与执行绑定'
                        """
                    )
                self._ensure_content_hash_index(connection)
                self._validate_schema(connection)
                connection.commit()
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise WorkflowStoreError("workflow database operation failed") from exc

    def upsert(self, definitions: Sequence[WorkflowDefinition]) -> int:
        """锁定并解析工作流身份后同步技术字段，保留管理员配置。"""

        if len(definitions) > 100:
            raise WorkflowStoreError("workflow import exceeds row limit")
        if not definitions:
            return 0
        rows = [_definition_to_row(definition) for definition in definitions]
        keys = [definition.workflow_key for definition in definitions]
        source_files = [definition.source_file for definition in definitions]
        hashes = [definition.content_sha256 for definition in definitions]
        if (
            len(set(keys)) != len(keys)
            or len(set(source_files)) != len(source_files)
            or len(set(hashes)) != len(hashes)
        ):
            raise WorkflowStoreError("workflow import identities are ambiguous")
        try:
            with closing(self._open()) as connection:
                self._validate_schema(connection)
                with connection.cursor() as cursor:
                    identities = self._load_existing_identities(cursor, definitions)
                    target_ids = self._resolve_target_ids(definitions, identities)
                    update_rows = [
                        _definition_to_update_row(definition, target_id)
                        for definition, target_id in zip(
                            definitions, target_ids, strict=True
                        )
                        if target_id is not None
                    ]
                    insert_rows = [
                        row
                        for row, target_id in zip(rows, target_ids, strict=True)
                        if target_id is None
                    ]
                    if update_rows:
                        cursor.executemany(
                            f"""
                            UPDATE `{self._table}`
                            SET source_file = %s,
                                source_format = %s,
                                ui_json = %s,
                                api_json = %s,
                                positive_node_id = %s,
                                negative_node_id = %s,
                                output_node_id = %s,
                                seed_node_id = %s,
                                content_sha256 = %s,
                                workflow_type = %s,
                                input_image_node_id = %s,
                                positive_input_name = %s,
                                negative_input_name = %s
                            WHERE id = %s
                            """,
                            update_rows,
                        )
                    if insert_rows:
                        cursor.executemany(
                            f"""
                        INSERT INTO `{self._table}` (
                            workflow_key, display_name, source_file, source_format,
                            ui_json, api_json, positive_node_id, negative_node_id,
                            output_node_id, seed_node_id, enabled, content_sha256,
                            workflow_type, input_image_node_id, positive_input_name,
                            negative_input_name
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                        )
                        """,
                            insert_rows,
                        )
                connection.commit()
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise WorkflowStoreError("workflow database operation failed") from exc
        return len(rows)

    def _load_existing_identities(
        self,
        cursor: Any,
        definitions: Sequence[WorkflowDefinition],
    ) -> tuple[_WorkflowIdentity, ...]:
        """锁定所有可能按 key、来源文件或内容哈希命中的既有记录。"""

        placeholders = ", ".join("%s" for _ in definitions)
        keys = tuple(definition.workflow_key for definition in definitions)
        source_files = tuple(definition.source_file for definition in definitions)
        hashes = tuple(definition.content_sha256 for definition in definitions)
        cursor.execute(
            f"""
            SELECT id, workflow_key, source_file, content_sha256
            FROM `{self._table}`
            WHERE workflow_key IN ({placeholders})
                OR source_file IN ({placeholders})
                OR content_sha256 IN ({placeholders})
            FOR UPDATE
            """,
            keys + source_files + hashes,
        )
        identities: list[_WorkflowIdentity] = []
        for row in cursor.fetchall():
            identity_id = row.get("id")
            workflow_key = row.get("workflow_key")
            source_file = row.get("source_file")
            content_sha256 = row.get("content_sha256")
            if (
                not isinstance(identity_id, int)
                or isinstance(identity_id, bool)
                or identity_id <= 0
                or not isinstance(workflow_key, str)
                or not isinstance(source_file, str)
                or not isinstance(content_sha256, str)
            ):
                raise WorkflowStoreError("workflow database row is invalid")
            identities.append(
                _WorkflowIdentity(
                    id=identity_id,
                    workflow_key=workflow_key,
                    source_file=source_file,
                    content_sha256=content_sha256,
                )
            )
        return tuple(identities)

    def _resolve_target_ids(
        self,
        definitions: Sequence[WorkflowDefinition],
        identities: Sequence[_WorkflowIdentity],
    ) -> tuple[int | None, ...]:
        """拒绝交叉身份与同批复用，返回确定的更新目标。"""

        by_key: dict[str, _WorkflowIdentity] = {}
        by_source_file: dict[str, _WorkflowIdentity] = {}
        by_hash: dict[str, _WorkflowIdentity] = {}
        for identity in identities:
            if (
                identity.workflow_key in by_key
                or identity.source_file in by_source_file
                or identity.content_sha256 in by_hash
            ):
                raise WorkflowStoreError("workflow database schema is invalid")
            by_key[identity.workflow_key] = identity
            by_source_file[identity.source_file] = identity
            by_hash[identity.content_sha256] = identity

        used_ids: set[int] = set()
        target_ids: list[int | None] = []
        for definition in definitions:
            key_match = by_key.get(definition.workflow_key)
            source_file_match = by_source_file.get(definition.source_file)
            hash_match = by_hash.get(definition.content_sha256)
            matches = {
                match.id
                for match in (key_match, source_file_match, hash_match)
                if match is not None
            }
            if len(matches) > 1:
                raise WorkflowStoreError("workflow import identities are ambiguous")
            target = key_match or source_file_match or hash_match
            target_id = target.id if target is not None else None
            if target_id is not None and target_id in used_ids:
                raise WorkflowStoreError("workflow import identities are ambiguous")
            if target_id is not None:
                used_ids.add(target_id)
            target_ids.append(target_id)
        return tuple(target_ids)

    def list_executable(self, *, limit: int = 20) -> tuple[WorkflowDefinition, ...]:
        """按稳定 key 返回已启用且含 API JSON 的有限工作流列表。"""

        if not 1 <= limit <= 20:
            raise ValueError("workflow list limit is invalid")
        try:
            with closing(self._open()) as connection:
                self._validate_schema(connection)
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM `{self._table}` "
                        "WHERE enabled = 1 AND api_json IS NOT NULL "
                        "ORDER BY workflow_key LIMIT %s",
                        (limit,),
                    )
                    rows = cursor.fetchall()
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise WorkflowStoreError("workflow database operation failed") from exc
        return tuple(_row_to_definition(row) for row in rows)

    def list_all(self, *, limit: int = 100) -> tuple[WorkflowDefinition, ...]:
        """供管理员命令和管理页面列出启用、停用及不可执行的工作流。"""

        if not 1 <= limit <= 100:
            raise ValueError("workflow list limit is invalid")
        try:
            with closing(self._open()) as connection:
                self._validate_schema(connection)
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM `{self._table}` "
                        "ORDER BY workflow_key LIMIT %s",
                        (limit,),
                    )
                    rows = cursor.fetchall()
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise WorkflowStoreError("workflow database operation failed") from exc
        return tuple(_row_to_definition(row) for row in rows)

    def get(self, workflow_key: str) -> WorkflowDefinition:
        """管理读取不按启用状态过滤，允许查看和修复停用记录。"""
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", workflow_key):
            raise WorkflowNotFoundError("workflow is unavailable")
        try:
            with closing(self._open()) as connection:
                self._validate_schema(connection)
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM `{self._table}` WHERE workflow_key = %s",
                        (workflow_key,),
                    )
                    row = cursor.fetchone()
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise WorkflowStoreError("workflow database operation failed") from exc
        if row is None:
            raise WorkflowNotFoundError("workflow is unavailable")
        return _row_to_definition(row)

    def list_page(
        self, *, after: str | None = None, page: int | None = None, limit: int = 10,
    ) -> tuple[tuple[WorkflowDefinition, ...], str | None, int]:
        """管理页支持直接页码；兼容游标仍按稳定工作流键前进。"""
        if not 1 <= limit <= 100 or (page is not None and (page < 1 or after is not None)):
            raise ValueError("workflow page limit is invalid")
        try:
            with closing(self._open()) as connection:
                self._validate_schema(connection)
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT COUNT(*) AS total FROM `{self._table}`")
                    total = cursor.fetchone()["total"]
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM `{self._table}` "
                        "WHERE workflow_key > %s ORDER BY workflow_key LIMIT %s"
                        + (" OFFSET %s" if page is not None else ""),
                        (after or "", limit + 1,
                         *(((page - 1) * limit,) if page is not None else ())),
                    )
                    rows = cursor.fetchall()
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise WorkflowStoreError("workflow database operation failed") from exc
        definitions = tuple(_row_to_definition(row) for row in rows[:limit])
        next_cursor = definitions[-1].workflow_key if len(rows) > limit else None
        return definitions, next_cursor, total

    def update_metadata(
        self, workflow_key: str, *, expected_revision: str, values: WorkflowMetadataPatch,
    ) -> WorkflowDefinition:
        """持有行锁比较完整版本，仅写业务列；同步与其他窗口不能穿过版本校验。"""
        try:
            with closing(self._open()) as connection:
                self._validate_schema(connection)
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM `{self._table}` "
                        "WHERE workflow_key = %s FOR UPDATE", (workflow_key,),
                    )
                    row = cursor.fetchone()
                    if row is None:
                        raise WorkflowNotFoundError("workflow is unavailable")
                    current = _row_to_definition(row)
                    if workflow_revision(current) != expected_revision:
                        raise WorkflowRevisionConflictError("workflow revision conflict")
                    candidate = replace(current, **values.model_dump(exclude_unset=True))
                    if values.enabled is True or candidate.workflow_type == "image_to_video":
                        validate_workflow_bindings(candidate)
                    cursor.execute(
                        f"UPDATE `{self._table}` SET display_name = %s, description = %s, "
                        "additional_positive_prompt = %s, additional_negative_prompt = %s, "
                        "enabled = %s WHERE workflow_key = %s",
                        (candidate.display_name, candidate.description,
                         candidate.additional_positive_prompt, candidate.additional_negative_prompt,
                         int(candidate.enabled), workflow_key),
                    )
                connection.commit()
                return candidate
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise WorkflowStoreError("workflow database operation failed") from exc

    def get_executable(self, workflow_key: str) -> WorkflowDefinition:
        """读取单个已启用的可执行工作流。"""

        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", workflow_key):
            raise WorkflowNotFoundError("workflow is unavailable")
        try:
            with closing(self._open()) as connection:
                self._validate_schema(connection)
                with connection.cursor() as cursor:
                    cursor.execute(
                        f"SELECT {_SELECT_COLUMNS} FROM `{self._table}` "
                        "WHERE workflow_key = %s AND enabled = 1 AND api_json IS NOT NULL",
                        (workflow_key,),
                    )
                    row = cursor.fetchone()
        except (InterfaceError, OperationalError, DatabaseError) as exc:
            raise WorkflowStoreError("workflow database operation failed") from exc
        if row is None:
            raise WorkflowNotFoundError("workflow is unavailable")
        definition = _row_to_definition(row)
        if not definition.executable:
            raise WorkflowNotFoundError("workflow is unavailable")
        return definition

    def _open(self) -> Any:
        """创建不自动提交的 utf8mb4 字典游标连接。"""

        return self._connect(
            host=self._params.host,
            port=self._params.port,
            user=self._params.user,
            password=self._params.password,
            database=self._params.database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=False,
        )

    def _validate_schema(self, connection: Any) -> None:
        """拒绝缺列或指向错误数据库的工作流表。"""

        columns = self._read_columns(connection)
        if not WORKFLOW_COLUMNS.issubset(columns):
            raise WorkflowStoreError("workflow database schema is invalid")
        if not self._content_hash_index_exists(connection):
            raise WorkflowStoreError("workflow database schema is invalid")

    def _add_missing_metadata_columns(self, connection: Any) -> None:
        """为旧表逐列补齐新增的工作流元数据字段。"""

        columns = self._read_columns(connection)
        with connection.cursor() as cursor:
            for column, definition in _METADATA_COLUMN_DEFINITIONS:
                if column not in columns:
                    cursor.execute(
                        f"ALTER TABLE `{self._table}` "
                        f"ADD COLUMN `{column}` {definition}"
                    )

    def _ensure_content_hash_index(self, connection: Any) -> None:
        """不存在内容唯一索引时保留最早记录并合并 enabled 状态。"""

        if self._content_hash_index_exists(connection):
            return
        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                UPDATE `{self._table}` AS retained
                INNER JOIN (
                    SELECT content_sha256, MIN(id) AS keep_id,
                        MIN(enabled) AS retained_enabled
                    FROM `{self._table}`
                    GROUP BY content_sha256
                    HAVING COUNT(*) > 1
                ) AS duplicate_group ON retained.id = duplicate_group.keep_id
                SET retained.enabled = duplicate_group.retained_enabled
                """
            )
            cursor.execute(
                f"""
                DELETE duplicate_row
                FROM `{self._table}` AS duplicate_row
                INNER JOIN `{self._table}` AS retained
                    ON retained.content_sha256 = duplicate_row.content_sha256
                    AND retained.id < duplicate_row.id
                """
            )
            cursor.execute(
                f"ALTER TABLE `{self._table}` ADD UNIQUE KEY "
                f"`{WORKFLOW_CONTENT_HASH_INDEX}` (`content_sha256`)"
            )

    def _content_hash_index_exists(self, connection: Any) -> bool:
        """校验内容索引恰为单列、完整长度的唯一索引。"""

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT INDEX_NAME, NON_UNIQUE, SEQ_IN_INDEX, COLUMN_NAME, SUB_PART
                FROM INFORMATION_SCHEMA.STATISTICS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = %s
                ORDER BY SEQ_IN_INDEX
                """,
                (
                    self._params.database,
                    self._table,
                    WORKFLOW_CONTENT_HASH_INDEX,
                ),
            )
            rows = cursor.fetchall()
        if not rows:
            return False
        valid = (
            len(rows) == 1
            and rows[0].get("NON_UNIQUE") == 0
            and rows[0].get("SEQ_IN_INDEX") == 1
            and str(rows[0].get("COLUMN_NAME", "")).lower() == "content_sha256"
            and rows[0].get("SUB_PART") is None
        )
        if not valid:
            raise WorkflowStoreError("workflow database schema is invalid")
        return True

    def _read_columns(self, connection: Any) -> set[str]:
        """读取目标库中工作流表的实际列名。"""

        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
                ORDER BY ORDINAL_POSITION
                """,
                (self._params.database, self._table),
            )
            rows = cursor.fetchall()
        return {str(row.get("COLUMN_NAME", "")).lower() for row in rows}


def _definition_to_row(definition: WorkflowDefinition) -> tuple[object, ...]:
    """转换新增参数，由数据库默认值初始化管理员配置字段。"""

    if not re.fullmatch(
        r"[a-z0-9][a-z0-9-]{0,127}", definition.workflow_key
    ) or not re.fullmatch(r"[0-9a-f]{64}", definition.content_sha256):
        raise WorkflowStoreError("workflow definition is invalid")
    return (
        definition.workflow_key,
        definition.display_name,
        definition.source_file,
        definition.source_format,
        definition.ui_json,
        definition.api_json,
        definition.positive_node_id,
        definition.negative_node_id,
        definition.output_node_id,
        definition.seed_node_id,
        int(definition.enabled),
        definition.content_sha256,
        definition.workflow_type,
        definition.input_image_node_id,
        definition.positive_input_name,
        definition.negative_input_name,
    )


def _definition_to_update_row(
    definition: WorkflowDefinition,
    target_id: int | None,
) -> tuple[object, ...]:
    """转换技术字段更新参数，不覆盖 key、启用状态和管理员配置。"""

    if target_id is None:
        raise WorkflowStoreError("workflow import identities are ambiguous")
    return (
        definition.source_file,
        definition.source_format,
        definition.ui_json,
        definition.api_json,
        definition.positive_node_id,
        definition.negative_node_id,
        definition.output_node_id,
        definition.seed_node_id,
        definition.content_sha256,
        definition.workflow_type,
        definition.input_image_node_id,
        definition.positive_input_name,
        definition.negative_input_name,
        target_id,
    )


def _row_to_definition(row: dict[str, Any]) -> WorkflowDefinition:
    """严格解析数据库行，避免无类型值流入 MCP。"""

    required_strings = (
        "workflow_key",
        "display_name",
        "source_file",
        "source_format",
        "content_sha256",
        "description",
        "additional_positive_prompt",
        "additional_negative_prompt",
    )
    if any(not isinstance(row.get(name), str) for name in required_strings):
        raise WorkflowStoreError("workflow database row is invalid")
    optional_strings = (
        "ui_json",
        "api_json",
        "positive_node_id",
        "negative_node_id",
        "output_node_id",
        "seed_node_id",
    )
    if any(
        row.get(name) is not None and not isinstance(row.get(name), str)
        for name in optional_strings
    ):
        raise WorkflowStoreError("workflow database row is invalid")
    enabled = row.get("enabled")
    if enabled not in {0, 1}:
        raise WorkflowStoreError("workflow database row is invalid")
    workflow_type = row.get("workflow_type", "text_to_image")
    if workflow_type not in {
        "text_to_image",
        "image_to_image",
        "fixed_reference",
        "image_to_video",
    } or any(
        not isinstance(row.get(name, "text"), str)
        for name in ("positive_input_name", "negative_input_name")
    ):
        raise WorkflowStoreError("workflow database bindings are invalid")
    return WorkflowDefinition(
        workflow_key=row["workflow_key"],
        display_name=row["display_name"],
        source_file=row["source_file"],
        source_format=row["source_format"],
        ui_json=row.get("ui_json"),
        api_json=row.get("api_json"),
        content_sha256=row["content_sha256"],
        positive_node_id=row.get("positive_node_id"),
        negative_node_id=row.get("negative_node_id"),
        output_node_id=row.get("output_node_id"),
        seed_node_id=row.get("seed_node_id"),
        enabled=bool(enabled),
        description=row["description"],
        additional_positive_prompt=row["additional_positive_prompt"],
        additional_negative_prompt=row["additional_negative_prompt"],
        workflow_type=workflow_type,
        input_image_node_id=row.get("input_image_node_id"),
        positive_input_name=row.get("positive_input_name", "text"),
        negative_input_name=row.get("negative_input_name", "text"),
    )
