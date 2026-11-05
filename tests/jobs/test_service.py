"""验证成功与明确失败不重复推理，中断任务可恢复且保留审计。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from tests.jobs.test_store import create, scope

from agent_bot.jobs.service import JobService


async def scheduled(job_store, job_clock):
    """创建一个已经到期的任务。"""
    job = await job_store.create(scope(), create())
    job_clock[0] += timedelta(seconds=2)
    return job


async def test_plain_model_once_and_success_waits_for_ack(job_store, job_clock):
    """生成结果通过同一投递接口读取，读取不会自动确认。"""
    job = await scheduled(job_store, job_clock)
    ai = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(content="任务完成")))
    service = JobService(job_store, ai)
    await asyncio.gather(service.run_due(), service.run_due())
    ai.complete.assert_awaited_once()
    request = ai.complete.call_args.args[0]
    assert request.model == "offline-model"
    assert request.options.max_tokens == 4096
    assert request.timeout_seconds == 120
    assert [message.role for message in request.messages] == ["system", "user"]
    assert request.messages[0].content == "可信系统提示"
    assert request.request_id == f"ai-job-{job.id}"
    assert (await service.pending_jobs("10001"))[0].result == "任务完成"
    await service.acknowledge_job("10001", job.id)
    assert await service.pending_jobs("10001") == ()


@pytest.mark.parametrize(
    "result",
    [None, " ", "x" * 64001, RuntimeError("private credentials")],
    ids=["missing", "blank", "oversized", "provider-error"],
)
async def test_model_failure_is_bounded_and_not_retried(job_store, job_clock, result):
    """供应商异常内容不入库，失败任务不重新调用模型。"""
    job = await scheduled(job_store, job_clock)
    ai = SimpleNamespace(complete=AsyncMock())
    if isinstance(result, Exception):
        ai.complete.side_effect = result
    else:
        ai.complete.return_value = SimpleNamespace(content=result)
    service = JobService(job_store, ai)
    await service.run_due()
    await service.run_due()
    value = await job_store.get(scope(), job.id)
    assert value.status == "FAILED"
    assert value.error_code == "MODEL_EXECUTION_FAILED"
    assert value.result is None
    ai.complete.assert_awaited_once()


async def test_worker_cancel_joins_provider_and_marks_interruption(job_store, job_clock):
    """关闭等待供应商释放资源，再回到待执行并保留中断历史。"""
    job = await scheduled(job_store, job_clock)
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def complete(request):
        """受控供应商在取消时释放资源。"""
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.set()

    service = JobService(job_store, SimpleNamespace(complete=complete))
    task = asyncio.create_task(service.run_due())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cleaned.is_set()
    assert (await job_store.get(scope(), job.id)).error_code == "EXECUTION_INTERRUPTED"
    assert (await job_store.get(scope(), job.id)).status == "PENDING"
    recovered_ai = SimpleNamespace(
        complete=AsyncMock(return_value=SimpleNamespace(content="重启后完成")),
    )
    await JobService(job_store, recovered_ai).run_due()
    assert (await job_store.get(scope(), job.id)).result == "重启后完成"
    history = await job_store.inspect(job.id)
    assert [item["status"] for item in history["attempts"]].count("INTERRUPTED") == 1
    assert [item["status"] for item in history["attempts"]].count("SUCCEEDED") == 1


async def test_cancelled_owner_not_overwritten_and_failed_commit_not_replayed(job_store, job_clock):
    """取消与写回故障均不导致再次生成。"""
    job = await scheduled(job_store, job_clock)

    async def complete(request):
        """模拟模型请求进行中由原发起人取消。"""
        await job_store.cancel(scope(), job.id)
        return SimpleNamespace(content="迟到结果")

    await JobService(job_store, SimpleNamespace(complete=complete)).run_due()
    assert (await job_store.get(scope(), job.id)).status == "CANCELLED"
    next_job = await job_store.create(scope("next"), create())
    job_clock[0] += timedelta(seconds=2)
    real_finish = job_store.finish
    job_store.finish = AsyncMock(side_effect=RuntimeError("db unavailable"))
    ai = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(content="成功但无法记录")))
    service = JobService(job_store, ai)
    with pytest.raises(RuntimeError):
        await service.run_due()
    job_store.finish = real_finish
    await service.run_due()
    assert (await job_store.get(scope(), next_job.id)).status == "RUNNING"
    ai.complete.assert_awaited_once()
