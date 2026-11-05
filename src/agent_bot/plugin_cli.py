"""插件独立进程入口；目录和端口只属于本次服务，不改其他实例的启动配置。"""

from __future__ import annotations

import argparse
import os
from collections.abc import Sequence
from pathlib import Path


def _port(value: str) -> int:
    """在创建工作目录或启动服务前检查监听端口范围。"""
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('端口必须是 1-65535 的整数') from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('端口必须是 1-65535 的整数')
    return port


def run(argv: Sequence[str]) -> int:
    """列表命令离线运行；run 在调用者当前进程中启动唯一指定插件。"""
    from agent_bot.plugins.catalog import BUILTIN_PLUGIN_IDS, BUILTIN_PLUGINS

    parser = argparse.ArgumentParser(prog='taobot plugin')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='列出可独立启动的内置插件')
    launch = commands.add_parser('run', help='在独立服务进程中运行指定插件')
    launch.add_argument('plugin_id', choices=sorted(BUILTIN_PLUGIN_IDS))
    launch.add_argument('--workdir', type=Path, required=True,
                        help='本进程独立的配置和数据目录；新目录首次启动自动初始化')
    launch.add_argument('--port', type=_port, help='仅覆盖本进程监听端口，不改启动文件')
    arguments = parser.parse_args(argv)
    if arguments.command == 'list':
        for entry in BUILTIN_PLUGINS:
            print(f'{entry.metadata.id}\t{entry.metadata.name}\t{entry.metadata.description}')
        return 0

    from agent_bot.__main__ import start_service

    directory = arguments.workdir.resolve()
    previous_directory, previous_port = Path.cwd(), os.environ.get('PORT')
    try:
        directory.mkdir(parents=True, exist_ok=True)
        os.chdir(directory)
    except OSError:
        print('独立插件启动失败：无法访问工作目录，请检查路径与权限。')
        return 1
    try:
        if arguments.port is not None:
            # 走现有环境覆盖边界，网页会把 CLI 端口显示为只读，不能写回陈旧文件值。
            os.environ['PORT'] = str(arguments.port)
        return start_service(standalone_plugin=arguments.plugin_id)
    finally:
        os.chdir(previous_directory)
        if previous_port is None:
            os.environ.pop('PORT', None)
        else:
            os.environ['PORT'] = previous_port
