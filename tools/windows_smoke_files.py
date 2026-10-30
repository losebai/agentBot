"""复制隔离桌面验收文件，排除 NapCat Node 载荷并保留窗口运行依赖。"""

import shutil
from pathlib import Path


def copy_without_qq_payload(bundle: Path, deployment: Path) -> None:
    """仅排除包顶层 runtime，Python.NET 的嵌套 runtime 必须保留。"""
    bundle = bundle.resolve()

    def ignore(directory: str, names: list[str]) -> set[str]:
        """按所属目录限定排除范围，避免同名依赖资源被误删。"""
        return {"runtime"} if Path(directory) == bundle and "runtime" in names else set()

    shutil.copytree(bundle, deployment, ignore=ignore)
