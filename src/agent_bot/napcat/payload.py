"""只校验固定 Shell 与完整摘要清单，本机 QQ 由启动边界单独定位。"""

import hashlib
import json
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

VERSION = "v4.18.42"
REQUIRED_FILES = frozenset({
    "shell/NapCatWinBootMain.exe", "shell/NapCatWinBootHook.dll", "shell/napcat.mjs",
    "shell/loadNapCat.js", "shell/qqnt.json",
})


class PayloadError(ValueError):
    """载荷不完整或不受支持，不携带底层文件内容。"""


@dataclass(frozen=True)
class NapCatPayload:
    """只保存经过目录和摘要检查的固定程序路径。"""

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
    """分块计算大体积 QQ 程序摘要，并及时释放文件句柄。"""
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


def quarantine_runtime_files(root: Path, residue: Path) -> list[str]:
    """启动前把 QQ 运行时落在载荷目录内的数据文件隔离到独立目录。

    QQ 运行会向程序目录写入 guild 数据库等残留，整体校验会把它们当成
    分发损坏；隔离保留而非删除，manifest 缺失或损坏时不动任何文件，
    交由 load_payload 明确报错。
    """
    try:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        files = manifest["files"]
        if not isinstance(files, dict):
            return []
    except (OSError, ValueError):
        return []
    declared = set(files) | {"manifest.json"}
    stamp = time.strftime("%Y%m%d-%H%M%S")
    moved = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in declared or is_link(path) or is_link(path.parent):
            continue
        target = residue / stamp / relative
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # 同盘改名原子生效；文件被占用时保持原状交给整体校验拒绝。
            os.replace(path, target)
            moved.append(relative)
        except OSError:
            continue
    return moved


def load_payload(root: Path) -> NapCatPayload:
    """执行前校验固定版本清单与全部摘要，拒绝混入额外运行文件。"""
    try:
        if is_link(root) or is_link(root / "manifest.json"):
            raise PayloadError("NapCat 载荷根目录不能是链接。")
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if (manifest["schema_version"] != 2 or manifest["platform"] != "windows-x64"
                or manifest["napcat_version"] != VERSION
                or manifest["entrypoint"] != "shell/NapCatWinBootMain.exe"
                or manifest["qq_source"] != "installed"):
            raise PayloadError("NapCat 载荷版本或平台不受支持。")
        files = manifest["files"]
        if not isinstance(files, dict) or not REQUIRED_FILES.issubset(files):
            raise PayloadError("NapCat 载荷缺少固定入口。")
        for name, digest in files.items():
            target = safe_payload_path(root, name)
            if (not name.startswith("shell/")
                    or any(part.casefold() in {"config", "data", "logs", "cache", "plugins"}
                           for part in PurePosixPath(name).parts[:-1])
                    or target.name.casefold().startswith((".env", "onebot11", "webui.json"))
                    or target.suffix.casefold() in {".db", ".sqlite", ".sqlite3", ".log"}
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
        if actual != set(files) | {"manifest.json"}:
            raise PayloadError("NapCat 载荷含有未声明文件。")
        return NapCatPayload(root.resolve(), manifest["napcat_version"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PayloadError("NapCat 载荷校验失败，请检查完整程序及 manifest.json。") from exc
