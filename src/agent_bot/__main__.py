"""统一启动管理服务，分发独立插件进程和离线管理命令。"""

import sys
from collections.abc import Sequence
from pathlib import Path


def main(argv: Sequence[str] | None = None) -> int:
    """离线命令先分发，避免初始化机器人或打开网络监听。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] in (["--help"], ["-h"]):
        print(
            "usage: taobot [database init/upgrade | records migrate | "
            "settings ... | workflows ... | worker [--memory-only | --autonomy] "
            "[--once | --inspect JOB_ID] | "
            "autonomy evaluate/serve | "
            "plugin list/run ... | --help]\n"
            "Without a subcommand, start the bot and browser management service."
        )
        return 0
    if arguments[:1] == ["settings"]:
        from agent_bot.config_cli import run

        return run(arguments[1:])
    if arguments[:1] == ["database"]:
        from agent_bot.database_cli import run

        return run(arguments[1:])
    if arguments[:1] == ['records']:
        from agent_bot.record_migration import run

        return run(arguments[1:])
    if arguments[:1] == ["workflows"]:
        from agent_bot.workflow_cli import run

        return run(arguments[1:])
    if arguments[:1] == ['plugin']:
        from agent_bot.plugin_cli import run

        return run(arguments[1:])
    if arguments[:1] == ["worker"]:
        from agent_bot.jobs.worker import run

        return run(arguments[1:])
    if arguments[:1] == ['autonomy']:
        from agent_bot.autonomy.cli import run

        return run(arguments[1:])
    if arguments:
        raise SystemExit(
            "usage: taobot [settings ... | workflows ... | worker ... | plugin ... | --help]"
        )
    return start_service()


def start_service(*, standalone_plugin: str | None = None) -> int:
    """公共入口与插件进程复用同一安全初始化、配置升级和退出编排。"""
    from agent_bot.admin.config import AdminConfig
    from agent_bot.bootstrap import load_startup_settings
    from agent_bot.cli_errors import describe_config_error
    from agent_bot.first_run import FirstRunError, prepare_first_run
    from agent_bot.settings import SettingsError

    try:
        if standalone_plugin is None:
            prepare_first_run(Path.cwd())
        else:
            prepare_first_run(Path.cwd(), enabled_plugins=[standalone_plugin])
        admin = AdminConfig.from_environment()
    except FirstRunError as exc:
        print(f"启动失败：{exc}")
        return 1
    except ValueError as exc:
        print(f"启动失败：请检查 AGENT_BOT_ADMIN_TOKEN。{describe_config_error(exc)}")
        return 1

    try:
        stored = load_startup_settings()
    except (ValueError, SettingsError) as exc:
        print(f"启动失败：{describe_config_error(exc)}")
        return 1
    from agent_bot.application import create_application

    if standalone_plugin is None:
        create_application(stored, admin)
    else:
        create_application(stored, admin, standalone_plugin=standalone_plugin)
    import nonebot

    nonebot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
