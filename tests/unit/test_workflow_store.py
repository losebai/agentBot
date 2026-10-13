from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from pymysql.err import OperationalError

from agent_bot.integrations.workflow_store import (
    WORKFLOW_COLUMNS,
    WORKFLOW_CONTENT_HASH_INDEX,
    WorkflowStore,
    WorkflowStoreError,
)
from agent_bot.services.workflows import WorkflowDefinition
from agent_bot.settings import DatabaseParams


class _Cursor:
    def __init__(self, database: _Database) -> None:
        self.database = database
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def execute(self, sql: str, args: object = None) -> int:
        self.database.executed.append((sql, args))
        normalized = " ".join(sql.split()).upper()
        description_match = re.search(
            r"MODIFY COLUMN DESCRIPTION VARCHAR\((\d+)\)",
            normalized,
        )
        if (
            description_match is not None
            and int(description_match.group(1))
            < self.database.minimum_description_capacity
        ):
            raise OperationalError(1406, "Data too long for column 'description'")
        if "INFORMATION_SCHEMA.COLUMNS" in normalized:
            self.rows = [{"COLUMN_NAME": column} for column in self.database.columns]
        elif "INFORMATION_SCHEMA.STATISTICS" in normalized:
            if self.database.index_rows is not None:
                self.rows = [dict(row) for row in self.database.index_rows]
            else:
                self.rows = [
                    {
                        "INDEX_NAME": index_name,
                        "NON_UNIQUE": 0,
                        "SEQ_IN_INDEX": 1,
                        "COLUMN_NAME": "content_sha256",
                        "SUB_PART": None,
                    }
                    for index_name in self.database.unique_indexes
                ]
        elif match := re.search(r"ADD COLUMN `([^`]+)`", sql, re.IGNORECASE):
            self.database.columns.add(match.group(1))
            self.rows = []
        elif match := re.search(r"ADD UNIQUE KEY `([^`]+)`", sql, re.IGNORECASE):
            self.database.unique_indexes.add(match.group(1))
            self.rows = []
        elif "FOR UPDATE" in normalized:
            self.rows = [dict(row) for row in self.database.identity_rows]
        elif normalized.startswith("SELECT"):
            self.rows = [dict(row) for row in self.database.selected_rows]
        else:
            self.rows = []
        return len(self.rows)

    def executemany(self, sql: str, args: list[tuple[object, ...]]) -> int:
        self.database.executed.append((sql, args))
        return len(args)

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows.pop(0) if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        rows, self.rows = self.rows, []
        return rows


class _Connection:
    def __init__(self, database: _Database) -> None:
        self.database = database

    def cursor(self) -> _Cursor:
        return _Cursor(self.database)

    def commit(self) -> None:
        self.database.commits += 1

    def rollback(self) -> None:
        self.database.rollbacks += 1

    def close(self) -> None:
        return None


class _Database:
    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        self.selected_rows: list[dict[str, Any]] = []
        self.identity_rows: list[dict[str, Any]] = []
        self.columns = set(WORKFLOW_COLUMNS)
        self.unique_indexes = {WORKFLOW_CONTENT_HASH_INDEX}
        self.index_rows: list[dict[str, Any]] | None = None
        self.minimum_description_capacity = 0
        self.commits = 0
        self.rollbacks = 0

    def connect(self, **_kwargs: object) -> _Connection:
        return _Connection(self)


def _definition(
    key: str = "sdxl-lightning-4step",
    content_sha256: str = "a" * 64,
    source_file: str = "workflow.json",
) -> WorkflowDefinition:
    """构造可通过存储层校验的工作流定义。"""

    return WorkflowDefinition(
        workflow_key=key,
        display_name="SDXL Lightning",
        source_file=source_file,
        source_format="ui",
        ui_json="{}",
        api_json='{"1":{"class_type":"PreviewImage","inputs":{}}}',
        content_sha256=content_sha256,
        positive_node_id="2",
        negative_node_id="3",
        output_node_id="7",
        seed_node_id="10",
        description="适合写实人像和产品摄影",
        additional_positive_prompt="high detail",
        additional_negative_prompt="low quality",
    )


def _store(database: _Database) -> WorkflowStore:
    return WorkflowStore(
        DatabaseParams(
            host="127.0.0.1",
            port=3306,
            user="chat",
            password="secret",
            database="chat",
        ),
        connect=database.connect,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("directory", [None, "/configured-workflows"])
async def test_startup_upgrades_old_workflow_columns_before_read_or_import(
    chat_plugin, monkeypatch: pytest.MonkeyPatch, directory: str | None,
) -> None:
    """旧库启用绘图后，启动必须先补列再读取目录或导入文件。"""
    from unittest.mock import Mock

    database = _Database()
    missing_columns = {
        "workflow_type", "input_image_node_id", "positive_input_name", "negative_input_name",
    }
    database.columns -= missing_columns
    monkeypatch.setattr(chat_plugin, "_workflow_store", _store(database))
    monkeypatch.setattr(chat_plugin, "plugin_config", chat_plugin.plugin_config.model_copy(update={
        "chat_local_workflow_enabled": True,
        "chat_local_workflow_directory": directory,
    }))
    monkeypatch.setattr(chat_plugin, "WorkflowCatalogImporter", Mock(return_value=Mock(
        scan=Mock(return_value=(_definition(),)),
    )))

    await chat_plugin.initialize_workflow_catalog()
    await chat_plugin.initialize_workflow_catalog()

    assert missing_columns.issubset(database.columns)
    statements = [statement for statement, _ in database.executed]
    alters = [statement for statement in statements if "ALTER TABLE" in statement]
    assert len(alters) == 4
    assert all("ADD COLUMN" in statement for statement in alters)
    assert all("MODIFY COLUMN" not in statement for statement in statements)
    assert all("DELETE" not in statement for statement in statements)
    assert all("CREATE TABLE" not in statement for statement in statements)


@pytest.mark.parametrize("missing", [{"id"}, set(WORKFLOW_COLUMNS)])
def test_startup_schema_rejects_invalid_base_before_altering(missing: set[str]) -> None:
    """基础表不存在或缺少核心结构时，不能擅自补成新表。"""
    database = _Database()
    database.columns -= missing

    with pytest.raises(WorkflowStoreError, match="schema is invalid"):
        _store(database).prepare_for_startup()

    assert not any("ALTER TABLE" in statement for statement, _ in database.executed)


def test_startup_schema_does_not_deduplicate_or_rebuild_indexes() -> None:
    """历史缺索引仍要求显式迁移，启动升级不能删改已有工作流。"""
    database = _Database()
    database.unique_indexes.clear()

    with pytest.raises(WorkflowStoreError, match="schema is invalid"):
        _store(database).prepare_for_startup()

    assert not any(
        keyword in statement
        for statement, _ in database.executed
        for keyword in ("ALTER TABLE", "UPDATE", "DELETE")
    )


def test_migrate_deduplicates_content_before_adding_unique_index() -> None:
    database = _Database()
    database.unique_indexes.clear()

    _store(database).migrate()

    statements = [statement for statement, _ in database.executed]
    sql = "\n".join(statements).upper()
    assert "CREATE TABLE IF NOT EXISTS" in sql
    assert "DROP" not in sql
    assert "MIN(ID)" in sql
    assert "MIN(ENABLED)" in sql
    assert "DELETE DUPLICATE_ROW" in sql
    assert "ADD UNIQUE KEY `UK_CHAT_WORKFLOW_CONTENT_SHA256`" in sql
    update_index = next(
        index for index, statement in enumerate(statements) if "MIN(enabled)" in statement
    )
    delete_index = next(
        index for index, statement in enumerate(statements) if "DELETE duplicate_row" in statement
    )
    unique_index = next(
        index
        for index, statement in enumerate(statements)
        if "ADD UNIQUE KEY `uk_chat_workflow_content_sha256`" in statement
    )
    assert update_index < delete_index < unique_index
    assert database.commits == 1


def test_migrate_keeps_duplicates_when_content_hash_index_already_exists() -> None:
    database = _Database()

    _store(database).migrate()

    sql = "\n".join(statement for statement, _ in database.executed).upper()
    assert "DELETE DUPLICATE_ROW" not in sql
    assert "ADD UNIQUE KEY `UK_CHAT_WORKFLOW_CONTENT_SHA256`" not in sql
    assert database.commits == 1


def test_migrate_rejects_expected_index_name_on_wrong_column() -> None:
    """同名索引若不严格约束内容哈希，迁移必须停止。"""

    database = _Database()
    database.index_rows = [
        {
            "INDEX_NAME": WORKFLOW_CONTENT_HASH_INDEX,
            "NON_UNIQUE": 0,
            "SEQ_IN_INDEX": 1,
            "COLUMN_NAME": "workflow_key",
            "SUB_PART": None,
        }
    ]

    with pytest.raises(WorkflowStoreError, match="schema is invalid"):
        _store(database).migrate()

    assert database.commits == 0


def test_migrate_adds_missing_metadata_columns_before_modifying_them() -> None:
    database = _Database()
    metadata_columns = {
        "description",
        "additional_positive_prompt",
        "additional_negative_prompt",
    }
    database.columns -= metadata_columns

    _store(database).migrate()

    statements = [statement for statement, _ in database.executed]
    for column in metadata_columns:
        add_index = next(
            index
            for index, statement in enumerate(statements)
            if f"ADD COLUMN `{column}`" in statement
        )
        modify_index = next(
            index
            for index, statement in enumerate(statements)
            if f"MODIFY COLUMN {column}" in statement
        )
        assert add_index < modify_index
    assert metadata_columns.issubset(database.columns)


def test_migrate_describes_table_and_every_column() -> None:
    database = _Database()

    _store(database).migrate()

    create_sql = database.executed[0][0]
    assert create_sql.count(" COMMENT '") == len(WORKFLOW_COLUMNS)
    assert "COMMENT='本地 Comfy 工作流定义与执行绑定'" in create_sql


def test_migrate_backfills_comments_on_existing_table() -> None:
    database = _Database()

    _store(database).migrate()

    executed_sql = "\n".join(statement for statement, _ in database.executed)
    assert "ALTER TABLE `chat_workflow`" in executed_sql
    assert executed_sql.count("MODIFY COLUMN") == len(WORKFLOW_COLUMNS)


def test_migrate_preserves_existing_description_capacity() -> None:
    """迁移不得缩窄现有描述列并导致合法历史数据阻断同步。"""

    database = _Database()
    database.minimum_description_capacity = 1_009

    _store(database).migrate()

    assert database.commits == 1


def test_migration_sql_describes_table_and_every_column() -> None:
    migration = (
        Path(__file__).parents[2]
        / "sql"
        / "legacy"
        / "2026-09-03-dispatch-chat-workflow.sql"
    ).read_text(encoding="utf-8")
    create_sql, separator, remaining_sql = migration.partition(";")

    assert separator
    assert create_sql.count(" COMMENT '") == len(WORKFLOW_COLUMNS)
    assert "COMMENT='本地 Comfy 工作流定义与执行绑定'" in create_sql
    assert remaining_sql.count("MODIFY COLUMN") == len(WORKFLOW_COLUMNS)
    assert "MIN(`id`) AS `keep_id`" in remaining_sql
    assert "MIN(`enabled`) AS `retained_enabled`" in remaining_sql
    assert "DELETE duplicate_row" in remaining_sql
    assert "uk_dispatch_chat_workflow_content_sha256" in remaining_sql
    assert "COMMENT='本地 Comfy 工作流定义与执行绑定'" in remaining_sql


def test_migration_sql_preserves_existing_description_capacity() -> None:
    """离线迁移脚本必须与运行时迁移一样避免缩窄描述列。"""

    migration = (
        Path(__file__).parents[2]
        / "sql"
        / "legacy"
        / "2026-09-03-dispatch-chat-workflow.sql"
    ).read_text(encoding="utf-8")

    assert "`description` VARCHAR(2000)" in migration


def test_upsert_inserts_new_rows_with_database_metadata_defaults() -> None:
    database = _Database()

    count = _store(database).upsert(
        (
            _definition(),
            _definition("ui-only", "b" * 64, "ui-only.json"),
        )
    )

    statement, rows = database.executed[-1]
    assert count == 2
    assert "INSERT INTO" in statement
    assert "ON DUPLICATE KEY UPDATE" not in statement
    assert "description" not in statement
    assert "additional_positive_prompt" not in statement
    assert "additional_negative_prompt" not in statement
    assert isinstance(rows, list)
    assert len(rows) == 2
    assert len(rows[0]) == 16
    assert database.commits == 1


def test_upsert_updates_hash_match_by_id_without_changing_key_or_enabled() -> None:
    """文件改名命中内容身份时，只按保留记录 ID 更新可变字段。"""

    database = _Database()
    database.identity_rows = [
        {
            "id": 17,
            "workflow_key": "retained-key",
            "source_file": "retained.json",
            "content_sha256": "a" * 64,
        }
    ]

    count = _store(database).upsert((_definition("renamed-key"),))

    statement, rows = database.executed[-1]
    update_clause = statement.partition("SET")[2].partition("WHERE")[0]
    assert count == 1
    assert "UPDATE `chat_workflow`" in statement
    assert "workflow_key" not in update_clause
    assert "enabled" not in update_clause
    assert "description" not in update_clause
    assert "additional_positive_prompt" not in update_clause
    assert "additional_negative_prompt" not in update_clause
    assert isinstance(rows, list)
    assert rows[0][-1] == 17
    assert database.commits == 1


def test_upsert_updates_existing_source_file_without_changing_admin_fields() -> None:
    """中文文件内容变化后仍更新原记录，并保留管理员配置。"""

    database = _Database()
    database.identity_rows = [
        {
            "id": 23,
            "workflow_key": "workflow-old",
            "source_file": "写实文生图.json",
            "content_sha256": "a" * 64,
        }
    ]

    count = _store(database).upsert(
        (_definition("workflow-new", "b" * 64, "写实文生图.json"),)
    )

    select_statement, select_args = next(
        (statement, args)
        for statement, args in database.executed
        if "FOR UPDATE" in statement
    )
    update_statement, rows = database.executed[-1]
    update_clause = update_statement.partition("SET")[2].partition("WHERE")[0]
    assert count == 1
    assert "source_file IN" in select_statement
    assert "写实文生图.json" in select_args
    assert "workflow_key" not in update_clause
    assert "display_name" not in update_clause
    assert "description" not in update_clause
    assert "additional_positive_prompt" not in update_clause
    assert "additional_negative_prompt" not in update_clause
    assert isinstance(rows, list)
    assert rows[0][-1] == 23


def test_upsert_rejects_key_and_hash_matching_different_rows() -> None:
    """key 与内容哈希分别命中不同记录时拒绝歧义写入。"""

    database = _Database()
    database.identity_rows = [
        {
            "id": 1,
            "workflow_key": "portrait",
            "source_file": "portrait.json",
            "content_sha256": "a" * 64,
        },
        {
            "id": 2,
            "workflow_key": "landscape",
            "source_file": "landscape.json",
            "content_sha256": "b" * 64,
        },
    ]

    with pytest.raises(WorkflowStoreError, match="ambiguous"):
        _store(database).upsert((_definition("portrait", "b" * 64),))

    assert database.commits == 0
    assert all(
        "INSERT INTO" not in statement and "UPDATE `chat_workflow`" not in statement
        for statement, _ in database.executed
    )


def test_upsert_rejects_batch_definitions_targeting_same_retained_row() -> None:
    """同批两项若分别按 key 和哈希命中同一旧记录，应整体拒绝。"""

    database = _Database()
    database.identity_rows = [
        {
            "id": 1,
            "workflow_key": "portrait",
            "source_file": "portrait.json",
            "content_sha256": "a" * 64,
        }
    ]

    with pytest.raises(WorkflowStoreError, match="ambiguous"):
        _store(database).upsert(
            (
                _definition("portrait", "b" * 64),
                _definition("renamed", "a" * 64),
            )
        )

    assert database.commits == 0


def test_get_executable_returns_typed_definition() -> None:
    database = _Database()
    database.selected_rows = [
        {
            "workflow_key": "sdxl-lightning-4step",
            "display_name": "SDXL Lightning",
            "source_file": "workflow.json",
            "source_format": "ui",
            "ui_json": "{}",
            "api_json": '{"1":{"class_type":"PreviewImage","inputs":{}}}',
            "content_sha256": "a" * 64,
            "positive_node_id": "2",
            "negative_node_id": "3",
            "output_node_id": "7",
            "seed_node_id": "10",
            "enabled": 1,
            "description": "适合写实人像和产品摄影",
            "additional_positive_prompt": "high detail",
            "additional_negative_prompt": "low quality",
            "workflow_type": "text_to_image",
            "positive_input_name": "text",
            "negative_input_name": "text",
            "input_image_node_id": None,
        }
    ]

    definition = _store(database).get_executable("sdxl-lightning-4step")

    assert definition == _definition()


def test_image_binding_columns_migrate_once_without_rewriting_rows():
    """旧库追加列，重复迁移不再 ADD，不触碰已有 JSON。"""
    columns = {"workflow_type", "input_image_node_id", "positive_input_name", "negative_input_name"}
    database = _Database()
    database.columns -= columns
    store = _store(database)
    store.migrate()
    store.migrate()
    sql = "\n".join(statement for statement, _ in database.executed)
    for column in columns:
        assert sql.count(f"ADD COLUMN `{column}`") == 1
    assert "FOREIGN KEY" not in sql
    assert "SET api_json" not in sql


def test_i2i_bindings_are_persisted_and_restored():
    """写入技术绑定不修改管理员名称和固定提示词。"""
    from dataclasses import asdict, replace

    definition = replace(
        _definition(), workflow_type="image_to_image", input_image_node_id="1",
        positive_input_name="value", negative_input_name="negative_prompt",
    )
    database = _Database()
    store = _store(database)
    store.upsert((definition,))
    sql, rows = database.executed[-1]
    assert "workflow_type" in sql
    assert rows[0][-4:] == ("image_to_image", "1", "value", "negative_prompt")
    database.selected_rows = [asdict(definition)]
    assert store.get_executable(definition.workflow_key) == definition
