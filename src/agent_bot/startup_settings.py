"""受字段白名单和文件版本控制的启动配置编辑，不改变运行中进程或数据库连接。"""

from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from agent_bot.settings_errors import (
    SettingsError,
    SettingsRevisionConflictError,
    SettingsValidationError,
)


class StartupSettingsError(SettingsError):
    """启动文件读写失败；公开错误不携带内容、路径或凭据。"""


class StartupSettingsConflictError(StartupSettingsError, SettingsRevisionConflictError):
    """启动文件在页面读取后已被其他管理窗口或原生端口操作修改。"""


class StartupSettingsValidationError(StartupSettingsError, SettingsValidationError):
    """写入不属于授权字段，或启动环境覆盖使文件编辑无法生效。"""


@dataclass(frozen=True, slots=True)
class StartupSettingsSnapshot:
    """内部明文仅供配置校验，管理响应须按字段权限脱敏后再公开版本。"""

    values: dict[str, str | None]
    revision: str


class StartupSettingsStore:
    """捕获启动环境优先级，以跨进程锁协调网页保存和桌面原生端口保存。"""

    def __init__(self, directory: Path, allowed_names: Iterable[str]) -> None:
        """固定本服务所属目录与可编辑字段；在环境清理前保留原始覆盖关系。"""
        self.directory = directory.resolve()
        self.allowed_names = frozenset(name.casefold() for name in allowed_names)
        self._environment = {name.casefold(): value for name, value in os.environ.items()
                             if name.casefold() in self.allowed_names}

    @property
    def editable_names(self) -> frozenset[str]:
        """无配置文件或环境变量优先时不虚假开放编辑，仍可读取实际启动值。"""
        if not self._target().is_file():
            return frozenset()
        return self.allowed_names - frozenset(self._environment)

    def read(self) -> StartupSettingsSnapshot:
        """读者也等待文件事务，避免把数据库尚未提交的暂存值显示为已保存。"""
        with self._locked():
            return self._read_unlocked()

    @contextmanager
    def edit(
        self, values: Mapping[str, Any], *, expected_revision: str,
    ) -> Iterator[StartupSettingsSnapshot]:
        """锁内暂存启动值；关联数据库保存失败时恢复原字节，成功后才释放读者。"""
        if not values or any(name not in self.allowed_names for name in values):
            raise StartupSettingsValidationError("启动配置包含不可编辑的字段。")
        if any(name in self._environment for name in values):
            raise StartupSettingsValidationError("该配置由环境变量指定，请先调整启动环境。")
        with self._locked():
            target = self._target()
            if not target.is_file():
                raise StartupSettingsError("启动配置文件尚未生成。")
            current = self._read_unlocked()
            if current.revision != expected_revision:
                raise StartupSettingsConflictError("启动配置已被修改，请重新加载。")
            try:
                original = target.read_bytes()
                text = original.decode("utf-8")
                content = "".join(
                    binding.original.string for binding in parse_stream(io.StringIO(text))
                    if binding.key is None or binding.key.casefold() not in values
                )
                content += "" if not content or content.endswith("\n") else "\n"
                content += "".join(
                    f"{name.upper()}={_dotenv_value(value)}\n" for name, value in values.items()
                )
                self._write_atomic(target, content.encode("utf-8"))
            except (OSError, UnicodeError) as exc:
                raise StartupSettingsError("启动配置无法保存，请检查文件权限。") from exc
            try:
                yield self._read_unlocked()
            except BaseException:
                try:
                    self._write_atomic(target, original)
                except OSError as exc:
                    raise StartupSettingsError(
                        "配置保存失败且启动文件未能恢复，请重新加载并核对启动配置。"
                    ) from exc
                raise

    def _target(self) -> Path:
        """写入与启动一致的最高优先级文件，不接受 HTTP 参数指定任意路径。"""
        prod = self.directory / ".env.prod"
        return prod if prod.exists() else self.directory / ".env"

    def _read_unlocked(self) -> StartupSettingsSnapshot:
        """两份启动文件都参与版本摘要，dotenv 禁止插值，环境覆盖保持启动时快照。"""
        digest = hashlib.sha256()
        values: dict[str, str | None] = {}
        try:
            for name in (".env", ".env.prod"):
                path = self.directory / name
                content = path.read_bytes() if path.exists() else None
                digest.update(name.encode("utf-8"))
                digest.update(b"missing" if content is None else str(len(content)).encode("ascii"))
                if content is not None:
                    digest.update(content)
                    parsed = dotenv_values(
                        stream=io.StringIO(content.decode("utf-8")), interpolate=False,
                    )
                    values.update({key.casefold(): value for key, value in parsed.items()
                                   if key.casefold() in self.allowed_names})
            values.update(self._environment)
        except (OSError, UnicodeError) as exc:
            raise StartupSettingsError("启动配置无法读取，请检查文件及权限。") from exc
        return StartupSettingsSnapshot(values=values, revision=digest.hexdigest())

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """锁文件只含一个占位字节；OS 在进程结束时释放锁，文件本身不存秘密。"""
        try:
            stream = (self.directory / ".env.edit.lock").open("a+b")
        except OSError as exc:
            raise StartupSettingsError("启动配置锁不可用，请检查目录权限。") from exc
        try:
            if os.name == "nt":
                import msvcrt

                if stream.seek(0, os.SEEK_END) == 0:
                    stream.write(b"\0")
                    stream.flush()
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                except OSError as exc:
                    raise StartupSettingsError("启动配置正在保存，请稍后重试。") from exc
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def _write_atomic(self, target: Path, content: bytes) -> None:
        """同目录原子替换，其他字段和文件权限保留，失败时释放临时凭据文件。"""
        # 使用既有 .env.* 忽略和打包排除规则，进程意外退出也不会纳入残留秘密文件。
        descriptor, temporary = tempfile.mkstemp(prefix=".env.edit-", dir=self.directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                # Windows 的 chmod 仅处理只读属性；写入秘密前复制原 DACL，替换不能放宽访问。
                if os.name == "nt":
                    _copy_windows_dacl(target, Path(temporary))
                stream.write(content)
            os.chmod(temporary, target.stat().st_mode)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)


def _copy_windows_dacl(source: Path, destination: Path) -> None:
    """通过系统 API 保留原 DACL 和继承边界，失败不替换原文件，也不增加平台依赖。"""
    import ctypes
    from ctypes import wintypes

    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    security.GetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        pointer, pointer, ctypes.POINTER(pointer), pointer, ctypes.POINTER(pointer),
    )
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.GetSecurityDescriptorControl.argtypes = (
        pointer, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD),
    )
    security.GetSecurityDescriptorControl.restype = wintypes.BOOL
    security.SetNamedSecurityInfoW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        pointer, pointer, pointer, pointer,
    )
    security.SetNamedSecurityInfoW.restype = wintypes.DWORD
    kernel.LocalFree.argtypes = (pointer,)
    kernel.LocalFree.restype = pointer
    dacl, descriptor = pointer(), pointer()
    # SE_FILE_OBJECT=1，DACL_SECURITY_INFORMATION=4；不读取需要特权的系统审核 ACL。
    error = security.GetNamedSecurityInfoW(
        str(source), 1, 4, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    try:
        control, revision = wintypes.WORD(), wintypes.DWORD()
        if not security.GetSecurityDescriptorControl(
            descriptor, ctypes.byref(control), ctypes.byref(revision),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # SE_DACL_PROTECTED=0x1000；设置同样的保护状态，避免从目录重新继承访问权限。
        inheritance = 0x80000000 if control.value & 0x1000 else 0x20000000
        error = security.SetNamedSecurityInfoW(
            str(destination), 1, 4 | inheritance, None, None, dacl, None,
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel.LocalFree(descriptor)


def _dotenv_value(value: Any) -> str:
    """编码已验证的标量及结构值，双引号和转义不允许扩展为额外启动字段。"""
    if value is None:
        return ""
    if isinstance(value, str):
        escapes = {"\\": "\\\\", '"': '\\"', "\a": "\\a", "\b": "\\b",
                   "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t", "\v": "\\v"}
        return '"' + value.translate(str.maketrans(escapes)) + '"'
    if isinstance(value, (dict, list)):
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return "'" + encoded.replace("\\", "\\\\").replace("'", "\\'") + "'"
    if isinstance(value, (bool, int, float)):
        return json.dumps(value, allow_nan=False)
    raise StartupSettingsValidationError("启动配置值不符合要求。")
