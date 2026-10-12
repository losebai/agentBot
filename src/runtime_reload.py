"""基于数据库 revision 的异步运行时配置刷新。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from agent_bot.database_errors import DatabaseFailure
from agent_bot.settings import SettingsError, VersionedSettings


class SettingsReader(Protocol):
    def load_revision(self) -> int: ...

    def load(self) -> VersionedSettings: ...


SettingsApplier = Callable[[VersionedSettings], Awaitable[None]]


class SettingsReloader:
    """仅在 revision 变化时加载并应用完整配置快照。"""

    def __init__(
        self,
        store: SettingsReader,
        *,
        initial_revision: int,
        apply: SettingsApplier,
    ) -> None:
        if type(initial_revision) is not int or initial_revision < 1:
            raise ValueError("initial settings revision must be positive")
        self._store = store
        self._revision = initial_revision
        self._apply = apply
        self._lock = asyncio.Lock()
        self._failure_stage: str | None = None

    @property
    def revision(self) -> int:
        return self._revision

    def failure_details(self, error: Exception) -> tuple[str, str, str]:
        """返回不包含配置值和底层驱动正文的刷新失败诊断。"""

        stage = self._failure_stage or "unknown"
        reason = (
            str(error)
            if isinstance(error, SettingsError)
            else "runtime settings apply failed"
        )
        if (failure := DatabaseFailure.from_error(error)) is not None:
            reason = f"{reason}; database={failure}"
        return stage, type(error).__name__, reason

    async def refresh_once(self) -> bool:
        """检查一次 revision；成功应用新快照时返回 True。"""

        async with self._lock:
            self._failure_stage = "revision_check"
            revision = await asyncio.to_thread(self._store.load_revision)
            if revision == self._revision:
                self._failure_stage = None
                return False

            self._failure_stage = "snapshot_load"
            stored = await asyncio.to_thread(self._store.load)
            if stored.revision == self._revision:
                self._failure_stage = None
                return False
            self._failure_stage = "runtime_apply"
            await self._apply(stored)
            self._revision = stored.revision
            self._failure_stage = None
            return True
