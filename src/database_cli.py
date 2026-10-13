"""离线初始化明确选择的数据库，不启动机器人或读取业务凭据。"""

import argparse
from collections.abc import Sequence

from agent_bot.bootstrap import database_params, load_bootstrap_config
from agent_bot.cli_errors import describe_config_error
from agent_bot.database import initialize_database
from agent_bot.settings import SettingsError


def run(argv: Sequence[str]) -> int:
    """只提供幂等初始化命令，数据库选择和失败均明确反馈。"""
    parser = argparse.ArgumentParser(prog="agent-bot database")
    parser.add_argument("command", choices=["init"])
    parser.parse_args(list(argv))
    try:
        params = database_params(load_bootstrap_config())
        initialize_database(params)
    except Exception as error:
        safe = SettingsError("database initialization failed")
        safe.__cause__ = error
        print(f"数据库初始化失败：{describe_config_error(safe)}")
        return 1
    print(f"{params.backend} 数据库结构已初始化；运行配置请执行 settings init。")
    return 0
