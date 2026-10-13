"""本地队列数据库的事务、取消和线程边界。"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable
from importlib.metadata import distribution
from pathlib import Path
from typing import TypeVar

_T = TypeVar("_T")


class SqliteQueueBusyError(RuntimeError):
    """仅表示可重试的 SQLite 写锁竞争，不包含结构或权限错误。"""


def queue_schema_sql() -> str:
    """从安装资源读取与发布包一致的独立队列结构。"""
    return distribution("agent-bot").locate_file(
        "agent_bot/resources/agent_bot_queue_init.sqlite.sql",
    ).read_text(encoding="utf-8")


def initialize_queue_database(path: str) -> None:
    """显式选择本地队列后创建文件和幂等结构，绝不触碰主配置库。"""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=5)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.executescript(queue_schema_sql())
    finally:
        connection.close()


def _transaction(path: str, callback: Callable[[sqlite3.Connection], _T]) -> _T:
    """只打开已初始化的文件，统一提交、回滚及释放连接。"""
    connection = sqlite3.connect(
        Path(path).expanduser().resolve().as_uri() + "?mode=rw", uri=True, timeout=5,
    )
    connection.row_factory = sqlite3.Row
    try:
        with connection:
            # 读改写共享同一写锁，去重、容量及取消合并不产生丢失更新。
            connection.execute("BEGIN IMMEDIATE")
            return callback(connection)
    except sqlite3.OperationalError as error:
        if getattr(error, "sqlite_errorcode", 0) & 0xFF in {
            sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
        }:
            raise SqliteQueueBusyError("本地队列数据库忙") from error
        raise
    finally:
        connection.close()


async def run_sqlite(path: str, callback: Callable[[sqlite3.Connection], _T]) -> _T:
    """在线程中执行完整事务，取消必须等提交或回滚结束才离开资源边界。"""
    task = asyncio.create_task(asyncio.to_thread(_transaction, path, callback))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # 工作线程不能被中途取消；等待它结束，避免停机后仍有隐式写入。
        await task
        raise
