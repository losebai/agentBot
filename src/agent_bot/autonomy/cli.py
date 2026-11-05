"""持续智能体的独立HTTP服务与无外联固定验收命令。"""

import argparse
import asyncio
import json
import logging
from collections.abc import Sequence

from agent_bot.autonomy.evaluation import run_fixed_suite


def run(argv: Sequence[str]) -> int:
    """显式serve启动独立宿主；evaluate仅使用临时测试库且失败返回非零。"""
    parser = argparse.ArgumentParser(prog="taobot autonomy")
    parser.add_argument("command", choices=["evaluate", "serve"])
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8081, choices=range(1, 65536), metavar='PORT')
    arguments = parser.parse_args(argv)
    if arguments.command == 'serve':
        import uvicorn

        from agent_bot.autonomy.host import create_host

        uvicorn.run(create_host(), host=arguments.host, port=arguments.port)
        return 0
    try:
        result = asyncio.run(run_fixed_suite())
    except Exception as error:
        logging.error("固定离线验收失败：error=%s", type(error).__name__)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] == result["total"] else 1
