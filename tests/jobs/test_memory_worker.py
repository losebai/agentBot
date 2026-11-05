"""独立记忆维护的节流、暂停、并发恢复和资源退出均使用离线数据。"""

import asyncio
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.jobs.test_integration import config

from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.jobs import worker as job_worker
from agent_bot.memory import worker
from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity
from agent_bot.settings import DatabaseParams

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def memory_store(tmp_path):
    """真实 SQLite 事务只访问临时目录，不连接生产数据库或模型。"""
    path = tmp_path / "memory-worker.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript((ROOT / "sql/agent_bot_init.sqlite.sql").read_text("utf-8"))
    params = DatabaseParams(backend="sqlite", sqlite_path=str(path))
    return TaggedMemoryStore(params, secret="offline-memory-worker-secret")


def settings_store(**changes):
    """权威配置替身可在每轮间切换，静态密钥不会来自本机环境。"""
    return SimpleNamespace(load=Mock(return_value=SimpleNamespace(config=config(**changes))))


def store_double():
    """维护替身只提供两个数据库操作，不含模型或渠道执行能力。"""
    return Mock(expire_due=Mock(return_value=0), record_daily_review=Mock(return_value={
        "date": "2026-11-04", "conflict_count": 0, "expiring_count": 0, "stale_candidates": [],
    }))


async def test_worker_throttles_maintenance_and_refreshes_pause_without_inference():
    """配置轮询期间不重复整理；暂停不写库，恢复立即检查，读配置失败不越过暂停。"""
    clock = [100.0]
    store = store_double()
    settings = settings_store(chat_config_refresh_interval_seconds=7)
    maintenance = worker.MemoryWorker(store, clock=lambda: clock[0])
    assert await maintenance.run_cycle(settings) == 7
    clock[0] += 3597
    assert await maintenance.run_cycle(settings) == 3
    store.expire_due.assert_called_once_with(limit=500)
    clock[0] += 3
    assert await maintenance.run_cycle(settings) == 7
    assert store.record_daily_review.call_count == 2
    settings.load.return_value.config = config(chat_tagged_memory_enabled=False)
    assert await maintenance.run_cycle(settings) == 5
    assert store.record_daily_review.call_count == 2
    settings.load.return_value.config = config(chat_tagged_memory_enabled=True)
    await maintenance.run_cycle(settings)
    assert store.record_daily_review.call_count == 3
    settings.load.side_effect = RuntimeError("offline settings unavailable")
    with pytest.raises(RuntimeError, match="offline settings unavailable"):
        await maintenance.run_cycle(settings)
    assert store.record_daily_review.call_count == 3


async def test_workers_share_expiration_and_review_with_restart(memory_store):
    """两个独立实例与重建后的实例复用事实版本和每日回执，不重复生成或遗忘。"""
    saved = memory_store.execute(
        MemoryIdentity(bot_id="1", user_id="2", session_id="s"),
        MemoryArguments(action="save", key="old", content="到期的约定", tags=["约定"],
                        valid_until=datetime.now(timezone.utc) - timedelta(seconds=1)),
    ).records[0]
    restored = TaggedMemoryStore(memory_store._params, secret=memory_store._secret)
    settings = settings_store()
    await asyncio.gather(
        worker.MemoryWorker(memory_store).run_cycle(settings),
        worker.MemoryWorker(restored).run_cycle(settings),
    )
    current = restored.admin_get(saved.id)
    assert current.status == "expired" and current.revision == 2
    assert current.valid_until == saved.valid_until
    history = restored.admin_reviews()["items"]
    assert len(history) == 1
    await worker.MemoryWorker(restored).run_cycle(settings)
    assert restored.admin_get(saved.id).revision == 2
    assert restored.admin_reviews()["items"] == history


async def test_review_failure_retries_without_duplicate_expiration(memory_store, monkeypatch):
    """到期事务已提交而复盘失败时，恢复不会再次推进版本；失败不占用小时调度。"""
    saved = memory_store.execute(
        MemoryIdentity(bot_id="1", user_id="2", session_id="s"),
        MemoryArguments(action="save", key="old", content="待到期事实", tags=["复盘"],
                        valid_until=datetime.now(timezone.utc) - timedelta(seconds=1)),
    ).records[0]
    original = memory_store.record_daily_review
    monkeypatch.setattr(memory_store, "record_daily_review",
                        Mock(side_effect=RuntimeError("offline review failure")))
    maintenance = worker.MemoryWorker(memory_store)
    settings = settings_store()
    with pytest.raises(RuntimeError, match="offline review failure"):
        await maintenance.run_cycle(settings)
    assert memory_store.admin_get(saved.id).revision == 2
    assert memory_store.admin_reviews()["items"] == []
    monkeypatch.setattr(memory_store, "record_daily_review", original)
    await maintenance.run_cycle(settings)
    assert memory_store.admin_get(saved.id).revision == 2
    assert len(memory_store.admin_reviews()["items"]) == 1


def install_service(monkeypatch, store):
    """装配入口使用替身连接池与启动配置，禁止启动升级访问本机文件。"""
    pool = SimpleNamespace(connect=Mock(), aclose=AsyncMock())
    settings = settings_store()
    monkeypatch.setattr(worker, "load_bootstrap_config", Mock())
    monkeypatch.setattr(worker, "database_params", Mock(return_value=DatabaseParams()))
    monkeypatch.setattr(worker, "load_startup_settings",
                        Mock(return_value=settings.load.return_value))
    monkeypatch.setattr(worker, "create_database_pool", Mock(return_value=pool))
    monkeypatch.setattr(worker, "TaggedMemoryStore", Mock(return_value=store))
    monkeypatch.setattr(worker, "SettingsStore", Mock(return_value=settings))
    return pool, settings


@pytest.mark.parametrize("fail", [False, True])
async def test_single_cycle_closes_pool_and_reports_failure(monkeypatch, fail):
    """单轮成功正常关闭，写入失败也先关闭连接池再返回错误。"""
    store = store_double()
    if fail:
        store.expire_due.side_effect = RuntimeError("offline expiration failure")
    pool, _ = install_service(monkeypatch, store)
    if fail:
        with pytest.raises(RuntimeError, match="offline expiration failure"):
            await worker.serve(once=True)
        store.record_daily_review.assert_not_called()
    else:
        await worker.serve(once=True)
        store.record_daily_review.assert_called_once_with()
    pool.aclose.assert_awaited_once()


async def test_cancellation_joins_database_before_closing_pool(monkeypatch):
    """已取消的常驻进程须等数据库线程释放连接，才能关闭池和结束进程。"""
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def expire_due(*, limit):
        """暂停数据库写线程，让测试观察取消时的资源关闭次序。"""
        loop.call_soon_threadsafe(started.set)
        assert release.wait(timeout=5)
        return 0

    store = store_double()
    store.expire_due.side_effect = expire_due
    pool, _ = install_service(monkeypatch, store)
    task = asyncio.create_task(worker.serve())
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        pool.aclose.assert_not_awaited()
    finally:
        release.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    pool.aclose.assert_awaited_once()
    store.record_daily_review.assert_not_called()


async def test_daemon_sleeps_after_error_then_reloads_disabled_configuration(monkeypatch):
    """常驻读取失败会休眠；下一轮以新的停用配置阻止写入，不沿用旧快照。"""
    store = store_double()
    pool, settings = install_service(monkeypatch, store)
    settings.load.side_effect = [
        RuntimeError("offline settings unavailable"),
        SimpleNamespace(config=config(chat_tagged_memory_enabled=False)),
    ]
    sleep = AsyncMock(side_effect=[None, asyncio.CancelledError()])
    monkeypatch.setattr(worker.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await worker.serve()
    assert sleep.await_count == 2
    assert all(call.args == (5,) for call in sleep.await_args_list)
    store.expire_due.assert_not_called()
    pool.aclose.assert_awaited_once()


def test_cli_memory_mode_never_constructs_ai_and_rejects_job_inspection(monkeypatch):
    """纯记忆模式只分发维护入口，互斥参数在任何数据库或模型装配前报错。"""
    serve = AsyncMock()
    ai = Mock(side_effect=AssertionError("memory mode must not construct AI"))
    monkeypatch.setattr(worker, "serve", serve)
    monkeypatch.setattr(job_worker, "AiService", ai)
    assert job_worker.run(["--memory-only", "--once"]) == 0
    serve.assert_awaited_once_with(once=True)
    ai.assert_not_called()
    with pytest.raises(SystemExit) as error:
        job_worker.run(["--memory-only", "--inspect", "a" * 32])
    assert error.value.code == 2


def test_cli_memory_failure_has_nonzero_status(monkeypatch):
    """维护失败明确返回非零退出码，进程守护器可据此识别单轮验收失败。"""
    monkeypatch.setattr(worker, "serve", AsyncMock(side_effect=RuntimeError("offline failure")))
    assert job_worker.run(["--memory-only", "--once"]) == 1


def test_real_cli_runs_without_qq_or_model_and_reuses_persisted_report(tmp_path):
    """独立进程通过真实 CLI、配置和临时库生成复盘，重复启动仍仅有一份报告。"""
    environment = {key: value for key, value in os.environ.items()
                   if not key.upper().startswith(("CHAT_", "ONEBOT_", "AGENT_BOT_"))}
    environment.update({
        "CHAT_DB_BACKEND": "sqlite", "CHAT_DB_SQLITE_PATH": str(tmp_path / "memory.sqlite3"),
        "ONEBOT_V11_ACCESS_TOKEN": "o" * 32, "CHAT_SESSION_SECRET": "s" * 32,
    })
    code = '''
import sys
from datetime import datetime, timedelta, timezone
from functools import partial
from pathlib import Path
from agent_bot.__main__ import main
from agent_bot import database_upgrade
from agent_bot.config import RuntimeConfig
from agent_bot.database import initialize_database
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.jobs import worker
from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity
from agent_bot.settings import DatabaseParams, SettingsStore

params = DatabaseParams(backend="sqlite", sqlite_path="memory.sqlite3")
initialize_database(params)
# 测试显式提供仓库资源，生产命令仍严格读取已安装发行包。
sql_root = Path(sys.argv[1])
database_upgrade.upgrade_database = partial(database_upgrade.upgrade_database,
    read_resource=lambda name: (sql_root / name).read_text("utf-8"))
config = RuntimeConfig(app_enabled_plugins=[], onebot_v11_access_token="o" * 32,
                       chat_session_secret="s" * 32)
SettingsStore(params, static_config=config).initialize(config)
store = TaggedMemoryStore(params, secret="s" * 32)
saved = store.execute(MemoryIdentity(bot_id="1", user_id="2", session_id="s"),
    MemoryArguments(action="save", key="old", content="expired fact", tags=["test"],
                    valid_until=datetime.now(timezone.utc) - timedelta(seconds=1))).records[0]
def forbidden_ai(*args, **kwargs):
    raise AssertionError("maintenance cannot construct AI")
worker.AiService = forbidden_ai
for _ in range(2):
    assert main(["worker", "--memory-only", "--once"]) == 0
assert "agent_bot.plugin.application" not in sys.modules
assert "agent_bot.application" not in sys.modules
assert len(store.admin_reviews()["items"]) == 1
assert store.admin_get(saved.id).status == "expired"
assert store.admin_get(saved.id).revision == 2
'''
    result = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / "sql")],
        cwd=tmp_path, env=environment, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
