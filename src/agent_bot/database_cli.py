"""离线初始化或升级明确选择的数据库，不启动机器人或平台连接。"""

import argparse
from collections.abc import Sequence

from agent_bot.bootstrap import database_params, load_bootstrap_config
from agent_bot.cli_errors import describe_config_error
from agent_bot.database import initialize_database
from agent_bot.database_upgrade import UpgradeError, upgrade_database
from agent_bot.settings import SettingsError


def run(argv: Sequence[str]) -> int:
    """初始化与升级分开；升级复用启动清单，不重新初始化已有业务表。"""
    parser = argparse.ArgumentParser(prog="agent-bot database")
    parser.add_argument("command", choices=['init', 'upgrade'])
    arguments = parser.parse_args(list(argv))
    try:
        params = database_params(load_bootstrap_config())
        if arguments.command == 'upgrade':
            result = upgrade_database(params)
            print(f'{params.backend} 数据库版本 {result.current_version}，'
                  f'本次执行 {len(result.applied)} 项升级。')
            return 0
        initialize_database(params)
    except UpgradeError as error:
        print(f'数据库升级失败：{describe_config_error(error)}')
        return 1
    except Exception as error:
        safe = SettingsError("database initialization failed")
        safe.__cause__ = error
        print(f"数据库初始化失败：{describe_config_error(safe)}")
        return 1
    print(f"{params.backend} 数据库结构已初始化；运行配置请执行 settings init。")
    return 0
