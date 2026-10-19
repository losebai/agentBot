"""停止服务并备份后，把长期记忆的授权类型、标签和素材引用合入事实表。"""

import argparse
import json
from contextlib import closing

import pymysql

from agent_bot.bootstrap import database_params, load_bootstrap_config
from agent_bot.database import connect_database
from agent_bot.integrations.tagged_memory_migration import (
    TaggedMemoryMigrationError,
    migrate_tagged_memory,
    verify_tagged_memory_migration,
)


def main() -> None:
    """连接唯一已选后端，输出计数；错误不泄露数据库凭据或事实原文。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", help="指定既有启动文件")
    parser.add_argument("--verify-only", action="store_true", help="只核对已迁移数据，不写入")
    parser.add_argument("--retire-legacy", action="store_true", help="核验后删除三张旧记忆关联表")
    args = parser.parse_args()
    if args.verify_only and args.retire_legacy:
        parser.error("核验模式不能同时退役旧表")
    try:
        params = database_params(load_bootstrap_config(env_file=args.env_file))
        with closing(connect_database(
            params, cursorclass=pymysql.cursors.DictCursor,
        )) as connection:
            counts = (
                verify_tagged_memory_migration(connection, backend=params.backend)
                if args.verify_only else
                migrate_tagged_memory(connection, backend=params.backend,
                                      retire_legacy=args.retire_legacy)
            )
            connection.rollback()
    except Exception as error:
        reason = (
            str(error) if isinstance(error, TaggedMemoryMigrationError) else type(error).__name__
        )
        parser.exit(1, f"长期记忆迁移失败：{reason}；请核对来源、结构和备份。\n")
    print(json.dumps(counts, ensure_ascii=False))


if __name__ == "__main__":
    main()
