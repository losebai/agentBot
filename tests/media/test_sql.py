"""以修改前的结构基线核对中文注释，测试不执行 MySQL DDL。"""

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
        for m in re.finditer(r"CREATE TABLE IF NOT EXISTS `?(\w+)`?\s*\((.*?)\)\s*ENGINE", sql, re.S)
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
    """合并不能遗漏自有表、改变既有约束，或在被引用表之前创建外键。"""
    sql = (ROOT / "sql/agent_bot_init.sql").read_text("utf-8")
    tables = definitions(sql)
    baseline = json.loads(Path(__file__).with_name("schema_baseline.json").read_text("utf-8"))
    for old in baseline.values():
        for name, declarations in definitions(old).items():
            assert name in tables, name
            assert normalized(",".join(tables[name])) == normalized(",".join(declarations)), name
    created = set()
    for name, declarations in tables.items():
        for declaration in declarations:
            for referenced in re.findall(r"REFERENCES\s+`?(\w+)`?", declaration, re.I):
                assert referenced in created, (name, referenced)
        created.add(name)
    assert len(tables) == 25


def test_unified_initialization_includes_memory_idempotency_and_workflow_uniqueness():
    """新库直接具备请求去重、会话隔离和内容去重，不依赖历史数据修补。"""
    tables = definitions((ROOT / "sql/agent_bot_init.sql").read_text("utf-8"))
    session = normalized(",".join(tables["dispatch_chat_memory_session"]))
    turns = normalized(",".join(tables["dispatch_chat_memory_turn"]))
    workflow = normalized(",".join(tables["dispatch_chat_workflow"]))
    assert "`generation_id` CHAR(32) NOT NULL" in session
    assert "`request_id` VARCHAR(128) COLLATE utf8mb4_bin NULL" in turns
    assert "UNIQUE KEY `uk_dispatch_chat_memory_turn_request`(`session_id`,`request_id`)" in turns
    assert "dispatch_chat_memory_receipt" in tables
    assert "UNIQUE KEY `uk_dispatch_chat_workflow_content_sha256`(`content_sha256`)" in workflow
    assert "`input_image_node_id` VARCHAR(64) NULL" in workflow
