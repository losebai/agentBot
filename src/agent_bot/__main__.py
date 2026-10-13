"""统一启动 agentBot 管理服务并分发离线配置和工作流命令。"""

import sys
from collections.abc import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """离线命令先分发，避免初始化机器人或打开网络监听。"""
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] in (["--help"], ["-h"]):
        print(
            "usage: agent-bot [database init | settings ... | workflows ... | --help]\n"
            "Without a subcommand, start the bot and browser management service."
        )
        return 0
    if arguments[:1] == ["settings"]:
        from agent_bot.config_cli import run

        return run(arguments[1:])
    if arguments[:1] == ["database"]:
        from agent_bot.database_cli import run

        return run(arguments[1:])
    if arguments[:1] == ["workflows"]:
        from agent_bot.workflow_cli import run

        return run(arguments[1:])
    if arguments:
        raise SystemExit("usage: agent-bot [settings ... | workflows ... | --help]")
    from agent_bot.admin.config import AdminConfig
    from agent_bot.bootstrap import load_startup_settings
    from agent_bot.cli_errors import describe_config_error
    from agent_bot.settings import SettingsError

    try:
        admin = AdminConfig.from_environment()
    except ValueError as exc:
        print(f"启动失败：请检查 AGENT_BOT_ADMIN_TOKEN。{describe_config_error(exc)}")
        return 1

    try:
        stored = load_startup_settings()
    except (ValueError, SettingsError) as exc:
        print(f"启动失败：{describe_config_error(exc)}")
        return 1
    from agent_bot.application import create_application

    create_application(stored, admin)
    import nonebot

    nonebot.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
