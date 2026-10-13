"""仅为无配置、无已有数据的本地首次启动生成独立配置与 SQLite 数据库。"""

from __future__ import annotations

import os
import secrets
import shutil
import tempfile
from pathlib import Path


class FirstRunError(RuntimeError):
    """首次初始化失败，只向启动入口提供不含底层凭据的修复提示。"""


def prepare_first_run(directory: Path) -> None:
    """离线暂存并独占发布本地默认配置，绝不覆盖用户配置或重建已有数据库。"""
    directory = directory.resolve()
    if any((directory / name).exists() for name in (".env", ".env.prod")):
        return

    from agent_bot.static_config import STATIC_SETTING_NAMES

    # 监听端口及日志级别常由启动器注入，其他静态环境代表用户管理的部署。
    deployment_fields = STATIC_SETTING_NAMES - {"host", "port", "log_level"}
    if any(
        name.casefold() in deployment_fields
        or name.upper().startswith(("CHAT_DB_", "CHAT_REDIS_", "AGENT_BOT_", "ONEBOT_"))
        or name.upper() in {"CHAT_QUEUE_BACKEND", "CHAT_QUEUE_SQLITE_PATH"}
        for name in os.environ
    ):
        return

    database_file = directory / "data" / "agent_bot.sqlite3"
    queue_file = directory / "data" / "queue.sqlite3"
    if database_file.exists() or queue_file.exists():
        raise FirstRunError("发现已有本地数据库但缺少配置，请恢复原 .env 配置后重启。")
    if any(directory.glob(".agent-bot-first-run-*")):
        raise FirstRunError(
            "发现中断的首次初始化，请检查 .agent-bot-first-run-* 暂存目录并恢复原配置；"
            "确认无数据需要保留后再移除暂存目录。"
        )

    staging: Path | None = None
    database_published = False
    config_published = False
    try:
        from agent_bot.config import RuntimeConfig
        from agent_bot.database import initialize_database
        from agent_bot.settings import DatabaseParams, SettingsStore

        staging = Path(tempfile.mkdtemp(prefix=".agent-bot-first-run-", dir=directory))
        config = RuntimeConfig(
            driver="~fastapi+~websockets",
            host="127.0.0.1",
            port=8080,
            onebot_v11_access_token=secrets.token_urlsafe(48),
            chat_session_secret=secrets.token_urlsafe(48),
        )
        env_text = (
            "# 首次启动生成的本地配置；密钥需保留，运行配置通过管理页面修改。\n"
            "CHAT_DB_BACKEND=sqlite\n"
            "CHAT_DB_SQLITE_PATH=data/agent_bot.sqlite3\n"
            "CHAT_QUEUE_BACKEND=sqlite\n"
            "CHAT_QUEUE_SQLITE_PATH=data/queue.sqlite3\n"
            "DRIVER=~fastapi+~websockets\n"
            "HOST=127.0.0.1\n"
            "PORT=8080\n"
            "AGENT_BOT_ADMIN_TOKEN=\n"
            f"ONEBOT_V11_ACCESS_TOKEN={config.onebot_v11_access_token.get_secret_value()}\n"
            f"CHAT_SESSION_SECRET={config.chat_session_secret.get_secret_value()}\n"
        )
        staged_env = staging / ".env"
        staged_env.write_text(env_text, encoding="utf-8")
        staged_database = staging / "agent_bot.sqlite3"
        params = DatabaseParams(backend="sqlite", sqlite_path=str(staged_database))
        initialize_database(params)
        SettingsStore(params, static_config=config).initialize(config)

        database_file.parent.mkdir(parents=True, exist_ok=True)
        # 同一文件系统的硬链接完整、独占地发布文件；并发启动不能覆盖对方结果。
        os.link(staged_database, database_file)
        database_published = True
        os.link(staged_env, directory / ".env")
        config_published = True
    except Exception as exc:
        if database_published:
            raise FirstRunError(
                "首次初始化失败：数据库已保存，请从 .agent-bot-first-run-* 暂存目录"
                "恢复 .env 配置后重启；已有文件未被覆盖。"
            ) from exc
        raise FirstRunError(
            "首次初始化失败，请检查目录写入权限、磁盘空间及是否有其他启动进程；"
            "已有配置和数据库未被覆盖。"
        ) from exc
    finally:
        # 发布中断后保留原密钥用于恢复；只清理本次创建且尚未使用的暂存文件。
        if staging is not None and (not database_published or config_published):
            shutil.rmtree(staging)
