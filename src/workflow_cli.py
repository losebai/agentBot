"""本地工作流数据库迁移、同步与检查命令。"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from agent_bot.bootstrap import database_params, load_bootstrap_config, load_static_config
from agent_bot.integrations.workflow_store import WorkflowStore
from agent_bot.services.workflows import WorkflowCatalogImporter
from agent_bot.settings import DatabaseParams, SettingsStore


def run(
    argv: Sequence[str],
    *,
    env_file: str | tuple[str, ...] | None = None,
) -> int:
    """执行离线工作流管理命令，错误只输出异常类型。"""

    parser = _build_parser()
    try:
        arguments = parser.parse_args(list(argv))
        store, params = _load_store(env_file)
        if arguments.command == "migrate":
            store.migrate()
            print("工作流表迁移完成。")
            return 0
        if arguments.command == "sync":
            raw_directory = arguments.directory
            if raw_directory is None:
                config = SettingsStore(
                    params, static_config=load_static_config(env_file=env_file),
                ).load().config
                raw_directory = config.chat_local_workflow_directory
            if raw_directory is None:
                raise ValueError("workflow directory is not configured")
            definitions = WorkflowCatalogImporter().scan(Path(raw_directory))
            store.migrate()
            count = store.upsert(definitions)
            executable = sum(definition.executable for definition in definitions)
            print(f"工作流同步完成：count={count} executable={executable}")
            return 0
        if arguments.command == "list":
            definitions = store.list_all()
            for definition in definitions:
                print(
                    f"{definition.workflow_key}\t"
                    f"enabled={str(definition.enabled).lower()}\t"
                    f"executable={str(definition.executable).lower()}\t"
                    f"name={definition.display_name}\t"
                    f"description={definition.description}"
                )
            print(f"count={len(definitions)}")
            return 0
    except (Exception, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code == 0:
            return 0
        print(f"工作流命令失败：{type(exc).__name__}")
        return 1
    return 1


def _build_parser() -> argparse.ArgumentParser:
    """构造工作流离线命令参数。"""

    parser = argparse.ArgumentParser(prog="chat workflows")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate")
    sync_parser = commands.add_parser("sync")
    sync_parser.add_argument("directory", nargs="?")
    commands.add_parser("list")
    return parser


def _load_store(
    env_file: str | tuple[str, ...] | None,
) -> tuple[WorkflowStore, DatabaseParams]:
    """仅用 bootstrap 凭据创建工作流存储，不依赖运行配置有效性。"""

    bootstrap = load_bootstrap_config(env_file=env_file)
    params = database_params(bootstrap)
    return WorkflowStore(params), params
