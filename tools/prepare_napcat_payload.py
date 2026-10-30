"""从固定 Node 运行目录生成干净载荷，不联网、启动 QQ 或读取本机资料。"""

import argparse
import json
import os
import shutil
import tempfile
from pathlib import Path

from agent_bot.napcat.payload import (
    NODE_ENTRY_SOURCE,
    NODE_VERSION,
    REQUIRED_FILES,
    VERSION,
    PayloadError,
    file_sha256,
    is_link,
    load_payload,
    safe_payload_path,
)


def prepare_payload(source: Path, destination: Path) -> None:
    """先定位源目录与入口缺失，再使用固定版本清单校验并发布干净资源。"""
    if destination.exists():
        raise FileExistsError("输出目录已存在，请使用新的目录。")
    source = source.absolute()
    if is_link(source):
        raise ValueError("载荷源目录不能是链接。")
    if not source.is_dir():
        raise ValueError("载荷源目录不存在或不是目录，请先准备包含 node/ 的程序目录。")
    source = source.resolve()
    destination = destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("载荷源目录和输出目录不能重叠。")
    # 入口缺失通常是未准备程序或解压层级错误，应在大文件复制前指出相对位置。
    missing = [name for name in sorted(REQUIRED_FILES)
               if name != "node/agentbot-entry.cjs"
               and not safe_payload_path(source, name).is_file()]
    if missing:
        raise PayloadError(f"载荷源目录缺少固定入口：{', '.join(missing)}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".napcat-payload-", dir=destination.parent,
    ) as temporary:
        stage = Path(temporary) / "payload"
        stage.mkdir()
        files = {}
        for path in source.rglob("*"):
            relative = path.relative_to(source).as_posix()
            safe_payload_path(source, relative)
            if path.is_file() and relative not in {"manifest.json", "node/agentbot-entry.cjs"}:
                target = stage / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                files[relative] = file_sha256(target)
        # 桥接代码由项目维护并进入摘要清单，源包不能替换原生资料目录边界。
        entry = stage / "node/agentbot-entry.cjs"
        shutil.copyfile(NODE_ENTRY_SOURCE, entry)
        files["node/agentbot-entry.cjs"] = file_sha256(entry)
        manifest = {"schema_version": 3, "platform": "windows-x64", "napcat_version": VERSION,
                    "runtime": "node", "node_version": NODE_VERSION,
                    "qq_source": "bundled-components", "entrypoint": "node/agentbot-entry.cjs",
                    "files": files}
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        load_payload(stage)
        # Windows rename 在目标已存在时失败，不会覆盖另一构建刚发布的载荷。
        os.rename(stage, destination)


def main() -> None:
    """构建者提供固定版本的本地资源；检查模式不会复制或执行程序。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        payload = load_payload(args.source)
        print(f"NapCat Node 载荷校验通过：{payload.napcat_version}；无需安装或启动系统 QQ。")
    else:
        if not args.destination:
            parser.error("准备载荷需要 destination")
        prepare_payload(args.source, args.destination)
        print("NapCat 载荷准备完成；仍需独立核实分发授权和真实登录兼容性。")


if __name__ == "__main__":
    main()
