"""验证有界数据库连接复用、事务隔离和资源关闭。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pymysql
import pytest

from agent_bot.settings import DatabaseParams


class PoolConnection:
    """保留事务状态的网络连接替身。"""

    def __init__(self, **options: object) -> None:
        """记录真实连接参数和事务状态。"""
        self.options = options
        self.open = True
        self.cursorclass = options.get("cursorclass", pymysql.cursors.Cursor)
        self.autocommit_mode = options.get("autocommit", False)
        self.pending: list[str] = []
        self.broken = False

    def rollback(self) -> None:
        """模拟断线或清空未提交事务。"""
        if self.broken or not self.open:
            raise pymysql.err.OperationalError(2013, "lost connection")
        self.pending.clear()

    def autocommit(self, value: bool) -> None:
        """切换连接事务模式。"""
        self.autocommit_mode = value

    def ping(self, reconnect: bool = True) -> None:
        """模拟仍持有 socket、但服务端已断开的空闲连接。"""
        assert reconnect is False
        if self.broken or not self.open:
            raise pymysql.err.OperationalError(2013, "lost connection")

    def close(self) -> None:
        """标记底层连接已关闭。"""
        self.open = False

    def fail(self, failure: str) -> None:
        """模拟底层断线或显式关闭。"""
        if failure == "broken":
            self.broken = True
        else:
            self.open = False


def _pool(*, connect=PoolConnection, **options: object):
    """构造不访问网络的真实连接池。"""
    from agent_bot.database_pool import MySqlConnectionPool

    return MySqlConnectionPool(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=connect,
        **options,
    )


def test_pool_reuses_connection_and_resets_transactions_and_cursor() -> None:
    """归还后不能把未提交数据、字典游标或自动提交模式泄漏给下一请求。"""
    pool = _pool(max_size=1)
    first = pool.connect(autocommit=True, cursorclass=pymysql.cursors.DictCursor)
    first.pending.append("uncommitted")
    options = first.options
    first.close()
    second = pool.connect(autocommit=False)
    assert second.options is options
    assert second.pending == []
    assert second.cursorclass is pymysql.cursors.Cursor
    assert second.autocommit_mode is False
    assert all(options[name] > 0 for name in ("connect_timeout", "read_timeout", "write_timeout"))
    second.close()
    pool.close()


def test_pool_full_acquisition_times_out_without_opening_extra_connection() -> None:
    """达到容量后等待有截止时间。"""
    pool = _pool(max_size=1, acquire_timeout_seconds=0.01)
    lease = pool.connect()
    with pytest.raises(pymysql.err.OperationalError, match="pool acquisition timed out"):
        pool.connect()
    lease.close()
    pool.close()


def test_pool_waiting_thread_resumes_when_connection_is_returned() -> None:
    """其他线程归还租用时唤醒等待者。"""
    pool = _pool(max_size=1)
    first = pool.connect()
    options = first.options
    started = Event()

    def acquire():
        """在线程中等待可用连接。"""
        started.set()
        return pool.connect()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(acquire)
        assert started.wait(1)
        first.close()
        second = future.result(timeout=2)
    assert second.options is options
    second.close()
    pool.close()


def test_pool_failed_connection_creation_releases_capacity() -> None:
    """建连失败不能永久占用唯一可用槽位。"""
    attempts = 0

    def connect(**options):
        """首轮模拟网络失败，第二轮返回可用连接。"""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise pymysql.err.OperationalError("database unavailable")
        return PoolConnection(**options)

    pool = _pool(max_size=1, connect=connect)
    with pytest.raises(pymysql.err.OperationalError):
        pool.connect()
    lease = pool.connect()
    assert lease.open
    lease.close()
    pool.close()


def test_pool_discards_connections_closed_while_idle() -> None:
    """归还后失效的空闲连接在下一次租用时被替换。"""
    pool = _pool(max_size=1)
    first = pool.connect()
    raw = first.rollback.__self__
    first.close()
    raw.close()
    second = pool.connect()
    assert second.open
    assert second.options is not raw.options
    second.close()
    pool.close()


def test_pool_replaces_disconnected_idle_socket_before_business_query() -> None:
    """socket 对象仍存在时，也不能把已经断开的连接交给下一次业务。"""
    pool = _pool(max_size=1)
    first = pool.connect()
    raw = first.rollback.__self__
    first.close()
    raw.broken = True

    second = pool.connect()
    try:
        second.rollback()
        assert not raw.open
        assert second.options is not raw.options
    finally:
        second.close()
        pool.close()


def test_pool_does_not_replay_an_active_transaction_after_disconnect() -> None:
    """已租出连接上的事务失败必须交回调用者，池不能透明重放写入。"""
    pool = _pool(max_size=1)
    lease = pool.connect()
    lease.fail("broken")
    with pytest.raises(pymysql.err.OperationalError):
        lease.rollback()
    lease.close()
    replacement = pool.connect()
    replacement.rollback()
    replacement.close()
    pool.close()


def test_pool_stale_connection_validation_obeys_acquisition_deadline(monkeypatch) -> None:
    """失效检测耗尽租用预算后不能继续创建连接或无界循环。"""
    from agent_bot import database_pool

    now = [0.0]
    monkeypatch.setattr(database_pool.time, "monotonic", lambda: now[0])
    pool = _pool(max_size=1, acquire_timeout_seconds=1)
    lease = pool.connect()
    raw = lease.rollback.__self__
    lease.close()

    def fail_ping(reconnect: bool) -> None:
        """模拟读超时消耗了全部剩余租用时间。"""
        assert reconnect is False
        now[0] = 2.0
        raise pymysql.err.OperationalError(2013, "lost connection")

    monkeypatch.setattr(raw, "ping", fail_ping)
    with pytest.raises(pymysql.err.OperationalError, match="pool acquisition timed out"):
        pool.connect()
    assert not raw.open
    # 失败释放容量，后续独立租用仍可恢复。
    replacement = pool.connect()
    replacement.rollback()
    replacement.close()
    pool.close()


def test_pool_shutdown_wakes_waiting_acquisition() -> None:
    """等待空闲连接的线程在关闭时立即收到关闭错误。"""
    pool = _pool(max_size=1)
    lease = pool.connect()
    started = Event()

    def acquire():
        """声明开始等待并请求租用。"""
        started.set()
        return pool.connect()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(acquire)
        assert started.wait(1)
        pool.close()
        with pytest.raises(pymysql.err.InterfaceError, match="pool is closed"):
            future.result(timeout=1)
    lease.close()


def test_pool_shutdown_during_creation_closes_new_connection() -> None:
    """建连过程中关闭池，不会把刚创建的连接泄漏给调用者。"""
    started = Event()
    release = Event()
    connections = []

    def connect(**options):
        """用事件模拟耗时建连。"""
        connection = PoolConnection(**options)
        connections.append(connection)
        started.set()
        assert release.wait(2)
        return connection

    pool = _pool(connect=connect)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(pool.connect)
        assert started.wait(1)
        pool.close()
        release.set()
        with pytest.raises(pymysql.err.InterfaceError, match="pool is closed"):
            future.result(timeout=1)
    assert connections[0].open is False


@pytest.mark.parametrize("options", [{"database": "other"}, {"ssl": object()}])
def test_pool_rejects_options_for_a_different_connection(options) -> None:
    """同一池禁止混入其他数据库或未支持连接选项。"""
    pool = _pool()
    with pytest.raises(ValueError, match="do not match"):
        pool.connect(**options)
    pool.close()


@pytest.mark.parametrize("failure", ["broken", "closed"])
def test_pool_discards_unusable_connections(failure: str) -> None:
    """断线或已关闭的底层连接不能重新进入空闲队列。"""
    pool = _pool(max_size=1)
    first = pool.connect()
    options = first.options
    first.fail(failure)
    first.close()
    second = pool.connect()
    assert second.options is not options
    second.close()
    pool.close()


@pytest.mark.asyncio
async def test_pool_close_rejects_new_work_and_closes_returned_leases() -> None:
    """关闭池会关闭空闲资源并在进行中租用归还时销毁连接。"""
    pool = _pool(max_size=2)
    idle = pool.connect()
    active = pool.connect()
    idle_raw = idle.rollback.__self__
    active_raw = active.rollback.__self__
    idle.close()
    await pool.aclose()
    assert idle_raw.open is False
    assert active_raw.open is True
    with pytest.raises(pymysql.err.InterfaceError, match="pool is closed"):
        pool.connect()
    active.close()
    active.close()
    assert active_raw.open is False
    with pytest.raises(pymysql.err.InterfaceError, match="lease is closed"):
        active.rollback()


@pytest.mark.parametrize("options", [{"max_size": 0}, {"acquire_timeout_seconds": 0},
                                        {"read_timeout_seconds": float("inf")}])
def test_pool_rejects_unbounded_or_invalid_limits(options: dict[str, object]) -> None:
    """配置必须确保池容量和等待、网络时限有界。"""
    with pytest.raises(ValueError):
        _pool(**options)
