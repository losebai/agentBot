"""增量与新库脚本必须一致，DDL 只静态核对、不连接数据库。"""

import re
from pathlib import Path

from tests.media.test_sql import definitions, normalized


def test_incremental_jobs_schema_is_comment_complete_and_matches_initialization():
    """确认只有新增表、中文表列说明，以及幂等和所有者外键约束。"""
    root = Path(__file__).resolve().parents[2]
    sql = (root / "sql/2026-10-12-python-jobs-auto-speak.sql").read_text("utf-8")
    tables = definitions(sql)
    unified = definitions((root / "sql/agent_bot_init.sql").read_text("utf-8"))
    assert set(tables) == {"agent_bot_job_owner", "agent_bot_job", "agent_bot_auto_speak_decision"}
    statements = re.sub(r"'[^']*'", "''", re.sub(r"--[^\n]*", "", sql))
    assert not re.search(r"\b(DROP|TRUNCATE|DELETE|INSERT|ALTER)\b", statements, re.I)
    assert len(re.findall(r"COMMENT='[^']*[\u4e00-\u9fff][^']*'", sql)) == 3
    for name, declarations in tables.items():
        assert normalized(",".join(declarations)) == normalized(",".join(unified[name]))
        for declaration in declarations:
            if declaration.strip().split()[0].upper() not in {
                "KEY",
                "PRIMARY",
                "UNIQUE",
                "CONSTRAINT",
            }:
                assert re.search(r"COMMENT '[^']*[\u4e00-\u9fff][^']*'", declaration)
    assert "UNIQUE KEY" in sql and "(deduplication_key)" in sql
    assert "REFERENCES agent_bot_job_owner" in sql
