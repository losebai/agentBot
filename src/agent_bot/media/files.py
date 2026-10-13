"""单进程独占、路径受控的媒体文件操作。"""

import hashlib
import os
import re
import stat
from pathlib import Path

from agent_bot.media.errors import MediaError

KEY = re.compile(r"[0-9a-f]{32}")
OWNED_NAME = re.compile(r"[0-9a-f]{32}(?:\.part)?")


def reject_links(path: Path) -> None:
    """逐级拒绝符号链接和 Windows 重解析点，目录联接不能逃逸根目录。"""
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise MediaError("MEDIA_STORAGE_UNSAFE", status_code=503)


class MediaFiles:
    """文件名仅由服务端随机 ID 构成，构造时不产生磁盘操作。"""

    def __init__(self, root: Path):
        """保留未解析路径，启动时检查每一级实际目录。"""
        self.root = Path(root).absolute()
        self._lock_file = None

    def start(self):
        """创建受控根并取得操作系统级独占锁；异常不遗留锁句柄。"""
        if self._lock_file is not None:
            return
        reject_links(self.root)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            reject_links(self.root)
            path = self.root / ".agentbot.lock"
            reject_links(path)
            handle = path.open("a+b")
            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BaseException:
                handle.close()
                raise
            self._lock_file = handle
        except OSError as error:
            raise MediaError("MEDIA_STORAGE_UNAVAILABLE", status_code=503) from error

    def close(self):
        """关闭句柄释放进程锁，不删除锁文件以免形成双重锁。"""
        if self._lock_file is not None:
            self._lock_file.close()
            self._lock_file = None

    def path(self, key: str, *, temporary=False) -> Path:
        """每次操作重新检查目录链；客户端名称从不作为路径使用。"""
        if self._lock_file is None or not KEY.fullmatch(key):
            raise MediaError("MEDIA_STORAGE_UNSAFE", status_code=503)
        path = self.root / (key + (".part" if temporary else ""))
        reject_links(path)
        if path.parent.resolve() != self.root.resolve():
            raise MediaError("MEDIA_STORAGE_UNSAFE", status_code=503)
        return path

    def write(self, key: str, data: bytes):
        """独占创建临时文件并刷盘，再在同目录原子替换。"""
        temporary = self.path(key, temporary=True)
        target = self.path(key)
        if target.exists():
            raise MediaError("MEDIA_SAVE_FAILED")
        with temporary.open("xb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        self.path(key, temporary=True)
        self.path(key)
        os.replace(temporary, target)

    def open(self, key: str, *, size: int, digest: str):
        """同一文件句柄校验完整性后归零，调用方持有租约直到关闭。"""
        try:
            file = self.path(key).open("rb")
        except FileNotFoundError as error:
            raise MediaError("MEDIA_FILE_CORRUPT") from error
        try:
            if os.fstat(file.fileno()).st_size != size:
                raise MediaError("MEDIA_FILE_CORRUPT")
            digest_state = hashlib.sha256()
            for chunk in iter(lambda: file.read(65536), b""):
                digest_state.update(chunk)
            calculated = digest_state.hexdigest()
            if calculated != digest:
                raise MediaError("MEDIA_FILE_CORRUPT")
            file.seek(0)
            return file
        except BaseException:
            file.close()
            raise

    def delete(self, key: str, *, temporary=False):
        """删除前再次校验受控目标，文件不存在视为幂等成功。"""
        self.path(key, temporary=temporary).unlink(missing_ok=True)

    def entries(self):
        """只枚举本服务命名的普通文件；用户文件不参与删除。"""
        reject_links(self.root)
        result = []
        for path in self.root.iterdir():
            if OWNED_NAME.fullmatch(path.name):
                reject_links(path)
                info = path.stat()
                if not stat.S_ISREG(info.st_mode):
                    raise MediaError("MEDIA_STORAGE_UNSAFE", status_code=503)
                result.append((path.name, info.st_size, info.st_mtime))
        return result
