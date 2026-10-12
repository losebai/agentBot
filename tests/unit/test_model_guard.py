import asyncio
from threading import Event

import pytest
from pymysql.err import OperationalError

from agent_bot.model_guard import ModelGuard, ModelGuardUnavailableError, ModelNotAvailableError
from agent_bot.settings import DatabaseParams


class FakeCursor:
    def __init__(self) -> None:
        self.parameters: list[tuple[str]] = []
        self.enabled = 1

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, query: str, parameters: tuple[str]) -> None:
        assert "type = 'chat'" in query
        self.parameters.append(parameters)

    def fetchone(self) -> tuple[int]:
        return (self.enabled,)


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> FakeCursor:
        return self._cursor

    def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_model_guard_checks_each_explicit_model_id() -> None:
    cursor = FakeCursor()
    params = DatabaseParams(
        host="127.0.0.1",
        port=3306,
        user="dispatch_chat",
        password="password",
        database="dispatch_chat",
    )
    guard = ModelGuard(
        params,
        connect=lambda **kwargs: FakeConnection(cursor),
    )

    await guard.ensure_enabled("chat-model")
    await guard.ensure_enabled("vision-model")
    await guard.ensure_enabled("image-model")
    await guard.ensure_enabled("audio-model")

    assert cursor.parameters == [
        ("chat-model",),
        ("vision-model",),
        ("image-model",),
        ("audio-model",),
    ]


@pytest.mark.asyncio
async def test_model_guard_exposes_database_failure_as_retryable_unavailability() -> None:
    params = DatabaseParams(
        host="127.0.0.1",
        port=3306,
        user="dispatch_chat",
        password="password",
        database="dispatch_chat",
    )

    def unavailable(**kwargs: object):
        raise OperationalError("database unavailable")

    guard = ModelGuard(params, connect=unavailable)

    with pytest.raises(ModelGuardUnavailableError):
        await guard.ensure_enabled("chat-model")


@pytest.mark.asyncio
async def test_model_guard_cache_expires_and_rechecks_disabled_models() -> None:
    """启用缓存有短期上限，过期后禁用状态立即拒绝。"""
    cursor = FakeCursor()
    now = [0.0]
    guard = ModelGuard(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=lambda **kwargs: FakeConnection(cursor),
        cache_ttl_seconds=2,
        clock=lambda: now[0],
    )
    await guard.ensure_enabled("model")
    await guard.ensure_enabled("model")
    assert len(cursor.parameters) == 1
    cursor.enabled = 0
    now[0] = 2.0
    with pytest.raises(ModelNotAvailableError):
        await guard.ensure_enabled("model")
    cursor.enabled = 1
    await guard.ensure_enabled("model")
    assert len(cursor.parameters) == 3


@pytest.mark.asyncio
async def test_model_guard_cache_is_bounded() -> None:
    """缓存不能随任意模型编号无限增加。"""
    cursor = FakeCursor()
    guard = ModelGuard(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=lambda **kwargs: FakeConnection(cursor),
        cache_max_size=1,
    )
    await guard.ensure_enabled("first")
    await guard.ensure_enabled("second")
    await guard.ensure_enabled("first")
    assert len(cursor.parameters) == 3


@pytest.mark.asyncio
async def test_zero_cache_ttl_queries_database_on_each_sequential_request() -> None:
    """TTL 为零关闭成功缓存，即使时钟未前进也立即读取最新禁用状态。"""
    cursor = FakeCursor()
    guard = ModelGuard(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=lambda **kwargs: FakeConnection(cursor),
        cache_ttl_seconds=0,
        clock=lambda: 0.0,
    )
    await guard.ensure_enabled("model")
    await guard.ensure_enabled("model")
    cursor.enabled = 0
    with pytest.raises(ModelNotAvailableError):
        await guard.ensure_enabled("model")
    assert cursor.parameters == [("model",), ("model",), ("model",)]


@pytest.mark.asyncio
async def test_model_guard_same_model_checks_share_one_query() -> None:
    """同模型并发检查共用一次查询，单个等待者取消不取消共享查询。"""
    cursor = FakeCursor()
    started = Event()
    release = Event()
    options = []

    def connect(**kwargs):
        """用事件控制网络查询开始和结束。"""
        options.append(kwargs)
        started.set()
        assert release.wait(2)
        return FakeConnection(cursor)

    guard = ModelGuard(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=connect,
    )
    first = asyncio.create_task(guard.ensure_enabled("model"))
    assert await asyncio.to_thread(started.wait, 1)
    second = asyncio.create_task(guard.ensure_enabled("model"))
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    await second
    assert len(cursor.parameters) == 1
    assert all(
        options[0][name] > 0 for name in ("connect_timeout", "read_timeout", "write_timeout")
    )


@pytest.mark.asyncio
async def test_model_guard_does_not_cache_database_failure() -> None:
    """数据库恢复后立即重新检查，不复用失败结果。"""
    cursor = FakeCursor()
    attempts = 0

    def connect(**kwargs):
        """模拟数据库首次不可达。"""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OperationalError("temporary failure")
        return FakeConnection(cursor)

    guard = ModelGuard(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=connect,
    )
    with pytest.raises(ModelGuardUnavailableError):
        await guard.ensure_enabled("model")
    await guard.ensure_enabled("model")
    assert len(cursor.parameters) == 1


@pytest.mark.asyncio
async def test_model_guard_bounds_pending_queries() -> None:
    """并发不同模型不能建立超出容量的后台检查。"""
    cursor = FakeCursor()
    started = Event()
    release = Event()

    def connect(**kwargs):
        """挂起唯一允许的网络查询。"""
        started.set()
        assert release.wait(2)
        return FakeConnection(cursor)

    guard = ModelGuard(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=connect,
        cache_max_size=1,
    )
    first = asyncio.create_task(guard.ensure_enabled("first"))
    assert await asyncio.to_thread(started.wait, 1)
    try:
        with pytest.raises(ModelGuardUnavailableError, match="busy"):
            await guard.ensure_enabled("second")
    finally:
        release.set()
        await first


@pytest.mark.parametrize("options", [{"cache_ttl_seconds": float("inf")}, {"cache_max_size": 0}])
def test_model_guard_rejects_unbounded_cache(options) -> None:
    """拒绝无限期缓存或无容量缓存配置。"""
    with pytest.raises(ValueError):
        ModelGuard(
            DatabaseParams(
                host="localhost", port=3306, user="test", password="test", database="test"
            ),
            **options,
        )
