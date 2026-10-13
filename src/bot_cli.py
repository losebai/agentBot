"""chat 命令使用的独立机器人入口。"""

import os
import sys
from collections.abc import Sequence

import nonebot
from nonebot.adapters.onebot.v11 import Adapter

from agent_bot.bootstrap import (
    inject_runtime_config,
    load_startup_settings,
    nonebot_init_kwargs,
    scrub_runtime_environment,
)


def run_bot() -> None:
    """从加密数据库加载配置后启动机器人。"""

    stored = load_startup_settings()
    scrub_runtime_environment()
    nonebot.init(
        _env_file=(os.devnull,),
        **nonebot_init_kwargs(stored.config),
    )
    driver = nonebot.get_driver()
    inject_runtime_config(driver.config, stored.config, revision=stored.revision)
    driver.register_adapter(Adapter)
    plugin = nonebot.load_plugin("agent_bot.plugin")
    if plugin is None:
        raise RuntimeError("failed to load agent_bot.plugin")
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
