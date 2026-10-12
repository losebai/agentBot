"""有容量、等待与网络时限的同步 MySQL 连接池。"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any, TypeVar

import pymysql
from pymysql.err import InterfaceError, OperationalError

from agent_bot.database_errors import DatabaseFailure, DatabasePoolTimeoutError
from agent_bot.settings import DatabaseParams

WriteResult = TypeVar("WriteResult")


async def finish_database_write(write: Callable[[], WriteResult]) -> WriteResult:
    """取消时等待已启动事务结束，防止调用方提前释放会话锁。"""

    task = asyncio.create_task(asyncio.to_thread(write))
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            cancellation = error
        except Exception:
            if cancellation is None:
                raise
    if cancellation is not None:
        # 取出已结束事务的异常，取消仍作为调用方观察到的最终结果。
        if not task.cancelled():
            task.exception()
        raise cancellation
    return task.result()


class _ConnectionLease:
    """保持 PyMySQL 调用形式，关闭租用时将连接归还所有者。"""

    def __init__(self, pool: MySqlConnectionPool, connection: Any) -> None:
        """绑定唯一池和本次租用的底层连接。"""
        self._pool = pool
        self._connection = connection
        self._closed = False

    def __getattr__(self, name: str) -> Any:
        """仅在租用有效期间转发连接操作。"""
        if self._closed:
            raise InterfaceError("database connection lease is closed")
        return getattr(self._connection, name)

    def close(self) -> None:
        """幂等归还连接，不允许重复增加池中可用数量。"""
        if not self._closed:
            self._closed = True
            self._pool._release(self._connection)


class MySqlConnectionPool:
    """跨线程复用同一数据库连接，所有游标及事务模式共享容量上限。"""

    def __init__(
        self,
        params: DatabaseParams,
        *,
        max_size: int = 8,
        acquire_timeout_seconds: float = 5,
        connect_timeout_seconds: float = 5,
        read_timeout_seconds: float = 10,
        write_timeout_seconds: float = 10,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        """保存有限资源策略，按需创建连接而不在初始化时联网。"""
        if not isinstance(max_size, int) or not 1 <= max_size <= 64:
            raise ValueError("database pool size must be between 1 and 64")
        for timeout in (
            acquire_timeout_seconds, connect_timeout_seconds,
            read_timeout_seconds, write_timeout_seconds,
        ):
            if not math.isfinite(timeout) or not 0 < timeout <= 300:
                raise ValueError("database pool timeouts must be finite and at most 300 seconds")
        self._params = params
        self._max_size = max_size
        self._acquire_timeout = acquire_timeout_seconds
        self._connect_timeout = connect_timeout_seconds
        self._read_timeout = read_timeout_seconds
        self._write_timeout = write_timeout_seconds
        self._factory = connect or pymysql.connect
        self._condition = threading.Condition()
        self._idle: list[Any] = []
        self._size = 0
        self._closed = False

    def connect(self, **kwargs: Any) -> _ConnectionLease:
        """有界等待租用，并按调用方需要恢复游标和事务模式。"""
        identity = {
            "host": self._params.host, "port": self._params.port,
            "user": self._params.user, "password": self._params.password,
            "database": self._params.database, "charset": "utf8mb4",
        }
        allowed = {*identity, "cursorclass", "autocommit", "connect_timeout",
                   "read_timeout", "write_timeout"}
        if any(key not in allowed for key in kwargs) or any(
            key in kwargs and kwargs[key] != value for key, value in identity.items()
        ):
            raise ValueError("connection options do not match this database pool")
        deadline = time.monotonic() + self._acquire_timeout
        while True:
            with self._condition:
                if self._closed:
                    raise InterfaceError("database pool is closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DatabasePoolTimeoutError("database pool acquisition timed out")
                if self._idle:
                    connection = self._idle.pop()
                    fresh = False
                elif self._size < self._max_size:
                    self._size += 1
                    connection = None
                    fresh = True
                else:
                    self._condition.wait(remaining)
                    continue
            try:
                if fresh:
                    connection = self._factory(
                        **identity,
                        connect_timeout=self._connect_timeout,
                        read_timeout=self._read_timeout,
                        write_timeout=self._write_timeout,
                        autocommit=False,
                    )
                elif not connection.open:
                    self._discard(connection)
                    continue
                else:
                    try:
                        connection.ping(reconnect=False)
                    except (InterfaceError, OperationalError) as error:
                        failure = DatabaseFailure.from_error(error)
                        if failure is None or not failure.transient:
                            raise
                        self._discard(connection)
                        continue
                connection.cursorclass = kwargs.get("cursorclass", pymysql.cursors.Cursor)
                connection.autocommit(bool(kwargs.get("autocommit", False)))
            except BaseException:
                self._discard(connection)
                raise
            with self._condition:
                closed = self._closed
            if closed:
                self._discard(connection)
                raise InterfaceError("database pool is closed")
            return _ConnectionLease(self, connection)

    def _release(self, connection: Any) -> None:
        """回滚未提交事务，重置模式，坏连接直接销毁。"""
        try:
            if not connection.open:
                self._discard(connection)
                return
            connection.rollback()
            connection.autocommit(False)
        except Exception:
            self._discard(connection)
            return
        with self._condition:
            if not self._closed:
                self._idle.append(connection)
                self._condition.notify()
                return
        self._discard(connection)

    def _discard(self, connection: Any | None) -> None:
        """销毁连接并释放容量，失败创建的占位同样释放。"""
        if connection is not None:
            with suppress(Exception):
                connection.close()
        with self._condition:
            self._size -= 1
            self._condition.notify()

    def close(self) -> None:
        """停止新租用并关闭空闲连接，活动连接在归还时关闭。"""
        with self._condition:
            self._closed = True
            idle, self._idle = self._idle, []
            self._condition.notify_all()
        for connection in idle:
            self._discard(connection)

    async def aclose(self) -> None:
        """在工作线程关闭同步连接，避免阻塞事件循环。"""
        await asyncio.to_thread(self.close)
