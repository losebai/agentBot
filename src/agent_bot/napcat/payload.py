"""校验独立 Node 运行包与完整摘要清单，不使用系统 QQ 安装。"""

import hashlib
import json
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

VERSION = "v4.18.38"
NODE_VERSION = "v22.11.0"
NODE_ENTRY_SOURCE = Path(__file__).with_name("node_entry.cjs")
# 官方包的终端依赖带有固定辅助程序，不能因此放行任意 QQ 或注入启动器。
ALLOWED_EXECUTABLES = frozenset({
    "node/node.exe", "node/napcat/native/pty/win32.x64/winpty-agent.exe",
})
REQUIRED_FILES = frozenset({
    "node/node.exe", "node/index.js", "node/agentbot-entry.cjs",
    "node/wrapper.node", "node/QQNT.dll",
    "node/package.json", "node/config.json", "node/napcat/napcat.mjs",
    "node/avif_convert.dll", "node/broadcast_ipc.dll", "node/crypto.dll", "node/ssl.dll",
    "node/libglib-2.0-0.dll", "node/libgobject-2.0-0.dll", "node/libvips-42.dll",
    "node/ncnn.dll", "node/opencv.dll", "node/QBar.dll", "node/LightQuic.dll",
    "node/win64/SSOShareInfoHelper64.dll", "node/win64/parent-ipc-core-x64.dll",
})


class PayloadError(ValueError):
    """载荷不完整或不受支持，不携带底层文件内容。"""


@dataclass(frozen=True)
class NapCatPayload:
    """只保存经过目录和摘要检查的固定 Node 程序路径。"""

    root: Path = field(repr=False)
    napcat_version: str


def is_link(path: Path) -> bool:
    """兼容包支持的 Python 版本，拒绝 Windows junction 等重解析入口。"""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(info, "st_file_attributes", 0) &
                                    stat.FILE_ATTRIBUTE_REPARSE_POINT)


def file_sha256(path: Path) -> str:
    """分块计算运行程序摘要，并及时释放文件句柄。"""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_payload_path(root: Path, name: str) -> Path:
    """拒绝 Windows 路径语法、目录链接及任何越界文件。"""
    parts = PurePosixPath(name).parts
    if (not parts or PurePosixPath(name).is_absolute() or ".." in parts
            or "\\" in name or ":" in name or str(PurePosixPath(name)) != name):
        raise PayloadError("NapCat 载荷路径无效。")
    target = root
    for part in parts:
        target = target / part
        if is_link(target):
            raise PayloadError("NapCat 载荷不能包含目录或文件链接。")
    if not target.resolve().is_relative_to(root.resolve()):
        raise PayloadError("NapCat 载荷路径越界。")
    return target


def load_payload(root: Path, *, runtime: bool = False) -> NapCatPayload:
    """校验全部程序摘要；运行时仅允许原生组件已确认生成的固定日志。"""
    try:
        if is_link(root) or is_link(root / "manifest.json"):
            raise PayloadError("NapCat 载荷根目录不能是链接。")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if (manifest["schema_version"] != 3 or manifest["platform"] != "windows-x64"
                or manifest["napcat_version"] != VERSION
                or manifest["node_version"] != NODE_VERSION
                or manifest["runtime"] != "node"
                or manifest["entrypoint"] != "node/agentbot-entry.cjs"
                or manifest["qq_source"] != "bundled-components"):
            raise PayloadError("NapCat 载荷版本或平台不受支持。")
        files = manifest["files"]
        if not isinstance(files, dict) or not REQUIRED_FILES.issubset(files):
            raise PayloadError("NapCat 载荷缺少固定入口。")
        for name, digest in files.items():
            target = safe_payload_path(root, name)
            if (not name.startswith("node/")
                    or any(part.casefold() in {"config", "data", "logs", "cache", "plugins"}
                           for part in PurePosixPath(name).parts[:-1])
                    or target.name.casefold().startswith((".env", "onebot11", "webui.json"))
                    or target.suffix.casefold() in {".db", ".sqlite", ".sqlite3", ".log"}
                    or (target.suffix.casefold() == ".exe" and name not in ALLOWED_EXECUTABLES)
                    or target.name.casefold() in {"napcatwinboothook.dll", "loadnapcat.js"}
                    or target.suffix.casefold() in {".bat", ".ps1"}
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise PayloadError("NapCat 载荷内容或摘要无效。")
            if file_sha256(target) != digest:
                raise PayloadError("NapCat 载荷摘要不匹配。")
        actual = set()
        for target in root.rglob("*"):
            name = target.relative_to(root).as_posix()
            safe_payload_path(root, name)
            if target.is_file():
                actual.add(name)
        # SDK 会在 node.exe 旁生成固定报告。它不参与执行，构建校验仍拒绝收集。
        if runtime:
            actual.discard("node/beacon_report.log")
        if actual != set(files) | {"manifest.json"}:
            raise PayloadError("NapCat 载荷含有未声明文件。")
        return NapCatPayload(root.resolve(), manifest["napcat_version"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PayloadError("NapCat 载荷校验失败，请检查完整程序及 manifest.json。") from exc
