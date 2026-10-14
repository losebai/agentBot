"""chat 命令使用的独立机器人入口。"""

import os
import sys
from collections.abc import Sequence

import nonebot

from agent_bot.bootstrap import (
    inject_runtime_config,
    load_startup_settings,
    nonebot_init_kwargs,
    scrub_runtime_environment,
)
from agent_bot.core.application import assemble_runtime


def run_bot() -> None:
    """合并启动静态值和数据库运行值，校验连接能力后启动机器人。"""

    stored = load_startup_settings()
    scrub_runtime_environment()
    nonebot.init(
        _env_file=(os.devnull,),
        **nonebot_init_kwargs(stored.config),
    )
    driver = nonebot.get_driver()
    inject_runtime_config(driver.config, stored.config, revision=stored.revision)
    assemble_runtime(stored, driver, getattr(driver, "server_app", None))
    nonebot.run()


def main(argv: Sequence[str] | None = None) -> int:
    """分发机器人运行与离线设置管理命令。"""

    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments[:1] == ["settings"]:
        from agent_bot.config_cli import run

        return run(arguments[1:])
    if arguments[:1] == ["workflows"]:
        from agent_bot.workflow_cli import run

        return run(arguments[1:])
    if arguments:
        raise SystemExit("usage: chat [settings ... | workflows ...]")
    run_bot()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
