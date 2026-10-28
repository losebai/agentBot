"""检查 wheel 包含所需模块及 SQL/UI 资源，不夹带凭据或运行数据。"""

import argparse
from pathlib import Path
from zipfile import ZipFile


def check(path: Path) -> None:
    """只读取指定 wheel，不安装或启动服务，也不执行 SQL。"""
    with ZipFile(path) as archive:
        names = archive.namelist()
        required = {
            'agent_bot/database_upgrade.py',
            'agent_bot/plugin_cli.py',
            'agent_bot/plugins/comfy/__init__.py',
            'agent_bot/plugins/comfy/plugin.py',
            'agent_bot/sql_scripts.py',
            'agent_bot/resources/2026-10-28-comfy-plugin.sql',
            'agent_bot/resources/2026-10-28-comfy-plugin.sqlite.sql',
            'agent_bot/resources/startup-versioning.sql',
            'agent_bot/resources/startup-versioning.sqlite.sql',
            'agent_bot/resources/UPGRADE-AUTOMATIC.md',
            'agent_bot/plugins/bilibili/plugin.py',
            'agent_bot/plugins/bilibili/tools.py',
            'agent_bot/resources/bilibili-init.sql',
            'agent_bot/resources/bilibili-init.sqlite.sql',
            'agent_bot/resources/UPGRADE-BILIBILI.md',
            "agent_bot/ai/model_reference.py",
            "agent_bot/integrations/model_identity_migration.py",
            "agent_bot/resources/2026-10-20-model-identity.sql",
            "agent_bot/resources/2026-10-20-model-identity.sqlite.sql",
            "agent_bot/resources/UPGRADE-MODEL-IDENTITY.md",
            "agent_bot/memory/contracts.py",
            "agent_bot/memory/embeddings.py",
            "agent_bot/integrations/memory_migration.py",
            "agent_bot/resources/2026-10-20-memory-lifecycle.sql",
            "agent_bot/resources/2026-10-20-memory-lifecycle.sqlite.sql",
            "agent_bot/resources/UPGRADE-MEMORY-LIFECYCLE.md",
            "agent_bot/__main__.py",
            "agent_bot/application.py",
            "agent_bot/ai/service.py",
            "agent_bot/ai/call_records.py",
            "agent_bot/record_migration.py",
            "agent_bot/session_context.py",
            "agent_bot/session_delivery.py",
            "agent_bot/session_schema.py",
            "agent_bot/session_migration.py",
            "agent_bot/resources/2026-10-19-session-context.sql",
            "agent_bot/resources/2026-10-19-session-context.sqlite.sql",
            "agent_bot/resources/2026-10-20-retire-legacy-session-context.sql",
            "agent_bot/resources/2026-10-20-retire-legacy-session-context.sqlite.sql",
            "agent_bot/resources/2026-10-20-merge-ai-sessions.sql",
            "agent_bot/resources/2026-10-20-merge-ai-sessions.sqlite.sql",
            "agent_bot/resources/2026-10-20-retire-ai-session.sql",
            "agent_bot/resources/2026-10-20-retire-ai-session.sqlite.sql",
            "agent_bot/integrations/tagged_memory_migration.py",
            "agent_bot/resources/2026-10-20-merge-tagged-memory.sql",
            "agent_bot/resources/2026-10-20-merge-tagged-memory.sqlite.sql",
            "agent_bot/resources/2026-10-20-retire-legacy-tagged-memory.sql",
            "agent_bot/resources/2026-10-20-retire-legacy-tagged-memory.sqlite.sql",
            "agent_bot/resources/2026-10-13-unified-call-records.sql",
            "agent_bot/resources/2026-10-13-unified-call-records.sqlite.sql",
            "agent_bot/resources/2026-10-13-agent-model-routing.sql",
            "agent_bot/resources/2026-10-13-agent-model-routing.sqlite.sql",
            "agent_bot/resources/2026-10-13-tool-call-payloads.sql",
            "agent_bot/resources/2026-10-13-tool-call-payloads.sqlite.sql",
            "agent_bot/agent/model_routing.py",
            "agent_bot/admin/streaming.py",
            "agent_bot/resources/agent_bot_init.sql",
            "agent_bot/resources/agent_bot_init.sqlite.sql",
            "agent_bot/resources/agent_bot_queue_init.sqlite.sql",
            "agent_bot/static_config.py",
            "agent_bot/database.py",
            "agent_bot/sqlite_database.py",
            "agent_bot/database_cli.py",
            "agent_bot/resources/2026-10-13-chat-index-names.sql",
            "agent_bot/resources/2026-10-13-dynamic-provider-mcp-credentials.sql",
            "agent_bot/resources/agent_bot_ai_init.sql",
            "agent_bot/resources/agent_bot_agent_init.sql",
            "agent_bot/resources/agent_bot_media_init.sql",
            "agent_bot/resources/2026-10-12-agentbot-schema-comments.sql",
            "agent_bot/resources/2026-10-12-python-jobs-auto-speak.sql",
            "agent_bot/jobs/service.py",
            "agent_bot/jobs/store.py",
            "agent_bot/jobs/tools.py",
            "agent_bot/ai/auto_speak.py",
            "agent_bot/ai/auto_speak_records.py",
            "agent_bot/media/service.py",
            "agent_bot/media/store.py",
            "agent_bot/admin/media_routes.py",
            "agent_bot/agent/service.py",
            "agent_bot/static/index.html",
            "agent_bot/bot_cli.py",
            "agent_bot/runtime_fields.py",
            "agent_bot/napcat/qq_installation.py",
            "agent_bot/resources/sdxl_lightning_4step_api.json",
            "agent_bot/resources/minimax_h3_r2v_upscale2x_api.json",
            "agent_bot/resources/minimax_h3_r2v_ultrasharp1080p_api.json",
            "agent_bot/resources/minimax_h3_r2v_ultrasharp720p_api.json",
            "agent_bot/resources/h3_frame_upscale.py",
            "agent_bot/resources/h3_temporal_consistency.py",
            "agent_bot/resources/minimax_h3_video_guide.md",
        }
        missing = required - set(names)
        if missing:
            raise ValueError(f"wheel resources missing: {sorted(missing)}")
        if any(name.startswith("dispatch_chat/") for name in names):
            raise ValueError("wheel contains the retired dispatch_chat package")
        if not any(
            name.startswith("agent_bot/static/assets/") and name.endswith(".js") for name in names
        ):
            raise ValueError("wheel has no built browser application")
        forbidden = {
            ".env", ".venv", "__pycache__", "node_modules", ".idea", "AGENTS.md",
            ".superpowers", "data", "logs", "tts", "qa-media", "qa-tts",
            "runtime",
        }
        if any(
            forbidden.intersection(Path(name).parts)
            or Path(name).suffix.lower() in {".log", ".wav", ".pcm", ".sqlite", ".sqlite3", ".db"}
            or Path(name).name.endswith(("-wal", "-shm", "-journal"))
            or Path(name).name.startswith(".env")
            for name in names
        ):
            raise ValueError("wheel contains excluded local artifacts")
        sql = archive.read("agent_bot/resources/agent_bot_init.sql").decode("utf-8")
        import re

        if re.search(r"\bDROP\b", sql, flags=re.IGNORECASE):
            raise ValueError("initialization SQL contains destructive statements")
        # 新库只使用统一建表脚本，禁止混入修改旧表或业务数据的历史迁移。
        statements = re.sub(r"--[^\n]*|'(?:''|[^'])*'", "", sql)
        if re.search(r"(?:^|;)\s*(ALTER|TRUNCATE|INSERT|UPDATE|DELETE)\b", statements, re.I):
            raise ValueError("unified initialization SQL contains migration statements")
        agent_sql = archive.read("agent_bot/resources/agent_bot_agent_init.sql").decode("utf-8")
        if re.search(r"\bDROP\b", agent_sql, flags=re.IGNORECASE):
            raise ValueError("agent initialization SQL contains destructive statements")
        for name in (
            "agent_bot_media_init.sql", "2026-10-12-agentbot-schema-comments.sql",
            "2026-10-12-python-jobs-auto-speak.sql",
        ):
            media_sql = archive.read(f"agent_bot/resources/{name}").decode("utf-8")
            if re.search(r"\b(DROP|TRUNCATE|DELETE\s+FROM)\b", media_sql, flags=re.IGNORECASE):
                raise ValueError("media SQL contains destructive statements")
            if not re.search(r"COMMENT\s+'[^']*[\u4e00-\u9fff]", media_sql):
                raise ValueError("media SQL has no Chinese comments")
        print(
            f"Wheel verified: {len(names)} entries, "
            "unified agent_bot and runtime/UI resources present."
        )


def main() -> None:
    """从命令行读取待检查产物路径。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    check(parser.parse_args().wheel)


if __name__ == "__main__":
    main()
