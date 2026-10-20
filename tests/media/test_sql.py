"""核对历史结构与明确的动态凭据增量，测试不执行真实 MySQL DDL。"""

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COMMENT = re.compile(r"\s+COMMENT\s*= ?\s*'[^']*'|\s+COMMENT\s+'[^']*'", re.I)


def definitions(sql):
    """提取本项目的有限建表语法，保留列和约束原始语义。"""
    sql = re.sub(r"--[^\n]*", "", sql)
    return {
        m[1]: re.split(r",(?![^()]*\))", m[2])
        for m in re.finditer(
            r"CREATE TABLE IF NOT EXISTS `?(\w+)`?\s*\((.*?)\)\s*ENGINE", sql, re.S
        )
    }


def normalized(value):
    """只忽略注释和 SQL 标点周围的空白，不忽略类型与排序规则。"""
    value = COMMENT.sub("", re.sub(r"--[^\n]*", "", value))
    return re.sub(r"\s*([(),;=])\s*", r"\1", re.sub(r"\s+", " ", value)).strip()


def test_owned_sql_comments_preserve_schema():
    """中文表列说明不得伴随既有结构的变化。"""
    baseline = json.loads((Path(__file__).with_name("schema_baseline.json")).read_text("utf-8"))
    tables = {}
    for filename, old in baseline.items():
        new = (ROOT / "sql/legacy" / filename).read_text("utf-8")
        assert normalized(new) == normalized(old)
        tables.update(definitions(new))
    tables.update(definitions((ROOT / "sql/legacy/agent_bot_media_init.sql").read_text("utf-8")))
    assert len(tables) == 13
    for name, declarations in tables.items():
        for column in declarations:
            if column.strip().split()[0].upper() in {"KEY", "PRIMARY", "UNIQUE", "CONSTRAINT"}:
                continue
            assert re.search(r"COMMENT\s+'[^']*[\u4e00-\u9fff][^']*'", column), (name, column)
    for filename in (*baseline, "agent_bot_media_init.sql"):
        sql = (ROOT / "sql/legacy" / filename).read_text("utf-8")
        assert len(re.findall(r"COMMENT\s*=\s*'[^']*[\u4e00-\u9fff][^']*'", sql)) == len(
            definitions(sql)
        )


def test_incremental_comments_do_not_redeclare_keys_or_change_column_types():
    """MODIFY 只更新列说明，不能再次声明主键或唯一索引。"""
    baseline = json.loads((Path(__file__).with_name("schema_baseline.json")).read_text("utf-8"))
    sql = (ROOT / "sql/2026-10-12-agentbot-schema-comments.sql").read_text("utf-8")
    assert "元数据锁" in sql
    alters = dict(re.findall(r"ALTER TABLE (\w+)\s+(.*?);", sql, re.S))
    assert len(alters) == 10
    for old in baseline.values():
        for name, declarations in definitions(old).items():
            for column in declarations:
                if column.strip().split()[0].upper() in {"KEY", "PRIMARY", "UNIQUE", "CONSTRAINT"}:
                    continue
                column = re.sub(r"\s+PRIMARY KEY|\s+UNIQUE\b", "", column, flags=re.I)
                assert normalized("MODIFY COLUMN " + column.strip()) in normalized(alters[name])
    statements = re.sub(r"'[^']*'", "''", re.sub(r"--[^\n]*", "", sql))
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|INSERT|PRIMARY KEY|UNIQUE)\b", statements, re.I)


def test_unified_initialization_preserves_owned_schema_and_orders_dependencies():
    """保留未退役业务结构及依赖顺序，旧普通执行结构只在历史归档中核对。"""
    sql = (ROOT / "sql/agent_bot_init.sql").read_text("utf-8")
    tables = definitions(sql)
    retired = definitions((ROOT / "sql/legacy/ai-session-v1.sql").read_text("utf-8"))
    assert set(retired) == {"agent_bot_ai_session"}
    assert "agent_bot_ai_session" not in tables
    baseline = json.loads(Path(__file__).with_name("schema_baseline.json").read_text("utf-8"))
    for old in baseline.values():
        for name, declarations in definitions(old).items():
            # 普通调用已合入统一归档，仍校验独立旧结构以保护历史迁移来源。
            declarations_now = retired[name] if name == "agent_bot_ai_session" else tables[name]
            # 标识符反引号只影响 SQL 解析，不改变列类型或约束。
            actual = normalized(",".join(declarations_now)).replace("`", "")
            expected = normalized(",".join(declarations)).replace("`", "")
            if name in {"agent_bot_ai_provider", "agent_bot_mcp"}:
                # 新库直接使用数据库凭据；既有库的旧列由独立增量脚本保留。
                expected = expected.replace(
                    "credential_ref VARCHAR(128)NULL",
                    "credential TEXT NULL,credential_required TINYINT NOT NULL DEFAULT 0",
                )
            if name == "agent_bot_agent":
                expected = expected.replace(
                    "model_id BIGINT UNSIGNED NOT NULL,",
                    "model_id BIGINT UNSIGNED NOT NULL,"
                    "vision_model_id BIGINT UNSIGNED NULL DEFAULT NULL,"
                    "fallback_model_id BIGINT UNSIGNED NULL DEFAULT NULL,",
                )
                expected += (
                    ",CONSTRAINT fk_agent_vision_model FOREIGN KEY(vision_model_id)"
                    "REFERENCES agent_bot_ai_model(id),"
                    "CONSTRAINT fk_agent_fallback_model FOREIGN KEY(fallback_model_id)"
                    "REFERENCES agent_bot_ai_model(id)"
                )
            if name == "agent_bot_agent_step":
                expected = expected.replace(
                    "summary VARCHAR(1000)NOT NULL,",
                    "summary VARCHAR(1000)NOT NULL,"
                    "arguments JSON NULL DEFAULT NULL,result JSON NULL DEFAULT NULL,",
                )
            assert actual == expected, name
    created = set()
    for name, declarations in tables.items():
        for declaration in declarations:
            for referenced in re.findall(r"REFERENCES\s+`?(\w+)`?", declaration, re.I):
                assert referenced in created, (name, referenced)
        created.add(name)
    # 明确列出完整发布基线，缺表或意外替换同数量表都应失败。
    assert set(tables) == {
        "chat_setting", "chat_setting_revision", "chat_group_member", "chat_group_context",
        "chat_tagged_memory", "chat_workflow", "agent_bot_ai_provider", "agent_bot_ai_model",
        "agent_bot_agent_run", "agent_bot_agent_step", "agent_bot_skill", "agent_bot_mcp",
        "agent_bot_agent", "agent_bot_agent_skill", "agent_bot_agent_mcp",
        "agent_bot_ai_model_media",
        "agent_bot_media_run", "agent_bot_media_asset", "agent_bot_job_owner", "agent_bot_job",
        "agent_bot_auto_speak_decision", "agent_bot_call_record", "chat_queue_jobs",
        "chat_queue_receipts", "chat_queue_deadletters", "chat_image_tasks", "chat_tool_operations",
        "chat_knowledge", "chat_knowledge_tag", "chat_material", "chat_material_source",
        "chat_session_context", "chat_memory_revision", "chat_memory_evidence",
        "chat_memory_operation", "chat_memory_embedding",
    }
    from agent_bot.memory.schema import MYSQL_ADDITIONAL

    # 新辅助表同样核对完整字段、主键、来源索引和外键，不能只增加预期表数量。
    for name, declarations in definitions(";\n".join(MYSQL_ADDITIONAL)).items():
        assert normalized(",".join(tables[name])) == normalized(",".join(declarations)), name
    assert not {"chat_memory_scope", "chat_memory_tag", "chat_memory_material"} & tables.keys()


def test_local_queue_increment_matches_full_initialization():
    """本地队列增量与新库结构一致，不修改已有业务表或数据。"""
    import sqlite3

    mysql = definitions((ROOT / "sql/agent_bot_init.sql").read_text("utf-8"))
    incremental = definitions((ROOT / "sql/2026-10-13-local-queue.sql").read_text("utf-8"))
    assert len(incremental) == 5
    for name, columns in incremental.items():
        assert normalized(",".join(mysql[name])) == normalized(",".join(columns))

    structures = []
    for name in ("agent_bot_init.sqlite.sql", "agent_bot_queue_init.sqlite.sql"):
        connection = sqlite3.connect(":memory:")
        try:
            script = (ROOT / "sql" / name).read_text("utf-8")
            connection.executescript(script)
            connection.executescript(script)
            placeholders = ",".join("?" for _ in incremental)
            structures.append(connection.execute(
                f"SELECT name,sql FROM sqlite_master WHERE tbl_name IN ({placeholders}) "
                "ORDER BY name", tuple(incremental),
            ).fetchall())
        finally:
            connection.close()
    assert structures[0] == structures[1]


def test_dynamic_credential_migration_only_adds_columns_and_preserves_auth_requirement():
    """升级不清空或重建表，旧引用保留为需要重新录入的鉴权配置。"""
    sql = (ROOT / "sql/2026-10-13-dynamic-provider-mcp-credentials.sql").read_text("utf-8")
    statements = re.sub(r"'[^']*'|--[^\n]*", "", sql)
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|INSERT)\b", statements, re.I)
    assert statements.count("ADD COLUMN credential TEXT NULL") == 2
    assert statements.count("ADD COLUMN credential_required TINYINT NOT NULL DEFAULT 0") == 2
    assert statements.count("credential_required = 1") == 2
    assert "revision = revision + 1" in statements
    assert "credential_ref IS NOT NULL" in statements


def test_legacy_index_migration_matches_initialization_and_preserves_data():
    """旧索引迁移保留数据，已退役会话表对照归档，其余表对照当前完整结构。"""
    sql = (ROOT / "sql/2026-10-13-chat-index-names.sql").read_text("utf-8")
    initial = (ROOT / "sql/agent_bot_init.sql").read_text("utf-8")
    statements = re.sub(r"--[^\n]*", "", sql)
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|UPDATE|INSERT)\b", statements, re.I)
    renames = re.findall(r"ALTER TABLE `(\w+)` RENAME INDEX `(\w+)` TO `(\w+)`", sql)
    assert len(renames) == 9
    tables = definitions(initial)
    legacy = definitions((ROOT / "sql/legacy/session-context-v1.sql").read_text("utf-8"))
    for table, old, new in renames:
        assert "dispatch_chat" in old
        if table in {"chat_memory_session", "chat_memory_turn", "chat_memory_receipt"}:
            assert table not in tables
            assert new in ",".join(legacy[table])
        else:
            assert new in ",".join(tables[table])
        assert f"INDEX_NAME = '{old}'" in sql and f"INDEX_NAME = '{new}'" in sql
    assert sql.count("AND NOT EXISTS") == len(renames)


def test_unified_initialization_includes_memory_idempotency_and_workflow_uniqueness():
    """新库在统一会话行保留代际与投递身份，工作流继续按内容去重。"""
    tables = definitions((ROOT / "sql/agent_bot_init.sql").read_text("utf-8"))
    session = normalized(",".join(tables["chat_session_context"]))
    workflow = normalized(",".join(tables["chat_workflow"]))
    assert "session_id VARCHAR(128)COLLATE utf8mb4_bin NOT NULL" in session
    assert "generation_id CHAR(32)COLLATE utf8mb4_bin NOT NULL" in session
    assert "messages_json MEDIUMTEXT NOT NULL" in session
    assert "deliveries_json MEDIUMTEXT NOT NULL" in session
    assert "PRIMARY KEY(session_id)" in session
    assert "expires_at" not in session
    assert not {"chat_memory_session", "chat_memory_turn", "chat_memory_receipt",
                "chat_sent_message"} & tables.keys()
    assert "UNIQUE KEY `uk_chat_workflow_content_sha256`(`content_sha256`)" in workflow
    assert "`input_image_node_id` VARCHAR(64)NULL" in workflow
