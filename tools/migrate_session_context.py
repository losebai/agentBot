"""停止服务并备份后，显式迁移旧会话到无过期的统一上下文表。"""

import argparse
import json

from agent_bot.bootstrap import database_params, load_bootstrap_config
from agent_bot.session_migration import migrate_session_context


def main() -> None:
    """读取既有启动配置选择唯一后端，失败仅输出异常类型，避免泄露连接凭据。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="指定启动文件，默认沿用 .env 和 .env.prod")
    parser.add_argument("--dry-run", action="store_true", help="只统计历史会话，不写入上下文")
    args = parser.parse_args()
    try:
        params = database_params(load_bootstrap_config(env_file=args.env_file))
        counts = migrate_session_context(params, dry_run=args.dry_run)
    except Exception as error:
        parser.exit(1, f"会话迁移失败：{type(error).__name__}；请核对备份、增量结构和启动配置。\n")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
