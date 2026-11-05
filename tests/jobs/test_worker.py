"""独立 worker 的休眠、配置暂停与真实文件库重启恢复，不调用外部服务。"""

import asyncio
import sqlite3
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from tests.jobs.test_integration import config
from tests.jobs.test_store import create, scope

from agent_bot.database import database_connector
from agent_bot.jobs import worker
from agent_bot.jobs.service import JobService
from agent_bot.jobs.store import JobStore
from agent_bot.settings import DatabaseParams

ROOT = Path(__file__).resolve().parents[2]


async def test_idle_and_paused_worker_never_infers_then_resumes(job_store, job_clock):
    """空闲和暂停不推理；恢复后刷新全局指令，原任务只生成一次。"""
    settings = SimpleNamespace(load=Mock(return_value=SimpleNamespace(
        config=config(chat_job_enabled=True, chat_mcp_enabled=True),
    )))
    provider = SimpleNamespace(system_prompt="旧全局提示词")
    ai = SimpleNamespace(
        complete=AsyncMock(return_value=SimpleNamespace(content="提醒到了")),
        providers={"openai-responses": provider},
    )
    service = JobService(job_store, ai)
    assert await worker.run_cycle(settings, service) == 5
    assert provider.system_prompt == settings.load.return_value.config.chat_system_prompt
    ai.complete.assert_not_awaited()
    job = await job_store.create(scope(), create())
    job_clock[0] += timedelta(seconds=2)
    settings.load.return_value.config = config()
    await worker.run_cycle(settings, service)
    assert (await job_store.get(scope(), job.id)).status == "PENDING"
    ai.complete.assert_not_awaited()
    settings.load.return_value.config = config(
        chat_job_enabled=True, chat_mcp_enabled=True, chat_active_actions_paused=True,
    )
    await worker.run_cycle(settings, service)
    assert (await job_store.get(scope(), job.id)).status == "PENDING"
    ai.complete.assert_not_awaited()
    settings.load.return_value.config = config(
        chat_job_enabled=True, chat_mcp_enabled=True, chat_system_prompt="更新后的全局提示词",
    )
    await worker.run_cycle(settings, service)
    assert provider.system_prompt == "更新后的全局提示词"
    settings.load.return_value.config = config(
        chat_job_enabled=True, chat_mcp_enabled=True, chat_system_prompt="",
    )
    await worker.run_cycle(settings, service)
    assert provider.system_prompt == ""
    ai.complete.assert_awaited_once()
    assert (await job_store.get(scope(), job.id)).result == "提醒到了"
    settings.load.side_effect = RuntimeError("offline database")
    with pytest.raises(RuntimeError):
        await worker.run_cycle(settings, service)
    ai.complete.assert_awaited_once()


async def test_file_database_restores_expired_claim_with_one_new_owner(tmp_path, job_clock):
    """独立连接重开文件库后恢复原任务；并发恢复只授予一份新租约。"""
    path = tmp_path / "worker.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript((ROOT / "sql/agent_bot_init.sqlite.sql").read_text("utf-8"))
    params = DatabaseParams(backend="sqlite", sqlite_path=str(path))

    def reopen():
        """新实例使用新的数据库连接，不共享进程内任务状态。"""
        return JobStore(params, connect=database_connector(params), clock=lambda: job_clock[0])

    original = reopen()
    job = await original.create(scope(), create())
    job_clock[0] += timedelta(seconds=2)
    first = await original.claim_due()
    assert await reopen().claim_due() is None
    job_clock[0] += timedelta(minutes=6)
    restored, competitor = reopen(), reopen()
    claims = await asyncio.gather(restored.claim_due(), competitor.claim_due())
    assert sum(claim is not None for claim in claims) == 1
    new = next(claim for claim in claims if claim is not None)
    assert new.id == job.id and new.session_id == first.session_id
    assert new.system_prompt == first.system_prompt
    assert not await original.finish(job.id, lease_token=first.lease_token, result="迟到")
    assert await restored.finish(job.id, lease_token=new.lease_token, result="恢复完成")
    assert (await reopen().pending_results("10001"))[0].result == "恢复完成"


@pytest.mark.parametrize("inspection", [False, True])
async def test_standalone_entry_releases_clients_and_inspection_never_builds_ai(
    monkeypatch, inspection,
):
    """单轮执行与本机审计均释放资源，审计不装配模型或机器人。"""
    pool = SimpleNamespace(connect=Mock(), aclose=AsyncMock())
    ai = SimpleNamespace(aclose=AsyncMock())
    construct_ai = Mock(return_value=ai)
    monkeypatch.setattr(worker, "load_bootstrap_config", Mock())
    monkeypatch.setattr(worker, "database_params", Mock(return_value=DatabaseParams()))
    monkeypatch.setattr(worker, "load_startup_settings", Mock(return_value=SimpleNamespace(
        config=config(),
    )))
    monkeypatch.setattr(worker, "create_database_pool", Mock(return_value=pool))
    monkeypatch.setattr(worker, "AiService", construct_ai)
    monkeypatch.setattr(worker, "run_cycle", AsyncMock(return_value=5))
    inspect = AsyncMock(return_value={"job": {}, "attempts": []})
    monkeypatch.setattr(worker, "JobStore", Mock(return_value=SimpleNamespace(inspect=inspect)))
    await worker.serve(once=not inspection, inspect_id="a" * 32 if inspection else None)
    pool.aclose.assert_awaited_once()
    if inspection:
        construct_ai.assert_not_called()
        inspect.assert_awaited_once_with("a" * 32)
    else:
        ai.aclose.assert_awaited_once()
