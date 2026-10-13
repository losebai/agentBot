"""在安装依赖前检查 Hatch 强制包含的资源，只使用 Python 标准库。"""

import argparse
import sys
from pathlib import Path

import tomllib


def check(root: Path) -> list[str]:
    """返回检出目录中缺失的强制资源，不启动服务或执行迁移。"""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    targets = project["tool"]["hatch"]["build"]["targets"]
    sources = {source for target in targets.values() for source in target.get("force-include", {})}
    return sorted(source for source in sources if not (root / source).exists())


def main() -> int:
    """输出全部缺失文件，避免依赖安装错误掩盖未提交的发布资源。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    root = parser.parse_args().root.resolve()
    missing = check(root)
    if missing:
        print("构建资源缺失：", file=sys.stderr)
        for source in missing:
            print(f"  {source}", file=sys.stderr)
        print("请将这些资源与打包配置一并提交到 Git，再重新运行流水线。", file=sys.stderr)
        return 1
    print("构建资源检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
