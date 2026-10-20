"""从已有独立业务库显式升级模型身份，默认仅预览。"""

import argparse
import json
from dataclasses import replace
from pathlib import Path

from agent_bot.bootstrap import database_params, load_bootstrap_config
from agent_bot.integrations.model_identity_migration import migrate_model_identity
from agent_bot.settings import DatabaseParams


def main() -> None:
    """必须显式给SQLite路径或启动文件；普通服务启动不会调用本命令。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-path")
    parser.add_argument("--env-file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="实际升级；省略时只预览")
    mode.add_argument("--verify-only", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if bool(args.sqlite_path) == bool(args.env_file):
        parser.error("请选择 --sqlite-path 或 --env-file 之一")
    if args.sqlite_path:
        params = DatabaseParams(backend="sqlite", sqlite_path=str(Path(args.sqlite_path).resolve()))
    else:
        startup_file = Path(args.env_file).resolve()
        if not startup_file.is_file():
            parser.error("指定启动文件不存在")
        params = database_params(load_bootstrap_config(env_file=str(startup_file)))
        if params.backend == "sqlite":
            path = Path(params.sqlite_path)
            params = replace(
                params,
                sqlite_path=str(
                    (path if path.is_absolute() else startup_file.parent / path).resolve()
                ),
            )
    if params.backend == "sqlite" and not Path(params.sqlite_path).is_file():
        parser.error("SQLite业务库不存在，迁移不会创建新数据库")
    result = migrate_model_identity(
        params,
        dry_run=args.dry_run or not (args.apply or args.verify_only),
        verify_only=args.verify_only,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
