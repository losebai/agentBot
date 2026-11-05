"""固定时序和并发下验证 Job 身份、幂等、抢占及消费确认。"""

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from agent_bot.ai.errors import AiError
from agent_bot.jobs.contracts import JobCommand, JobScope


def scope(request="request-1", **changes):
    """模拟宿主提供的可信 QQ 会话，模型参数不含这些字段。"""
    return JobScope.model_validate(
        {
            "bot_id": "10001",
            "session_id": "session-1",
            "sender_id": "10002",
            "scene": "group",
            "group_id": "10003",
            "request_id": request,
            "model": "offline-model",
            "system_prompt": "可信系统提示",
            **changes,
        }
    )


def create(**changes):
    """构造一次性延时命令。"""
    return JobCommand.model_validate(
        {"action": "create", "prompt": "稍后提醒", "delaySeconds": 1, **changes}
    )


@pytest.mark.parametrize(
    "values",
    [
        {"delaySeconds": True},
        {"delaySeconds": 1.5},
        {"delaySeconds": 0},
        {"delaySeconds": 31536001},
        {"prompt": " "},
        {"prompt": "x" * 8001},
        {"runAt": "2026-10-13T12:00:00Z"},
        {"botId": "99999"},
    ],
)
def test_invalid_commands_rejected(values):
    """禁止数值强转、模糊时间和模型注入身份。"""
    with pytest.raises(ValidationError):
        create(**values)


async def test_owner_request_deduplicates_concurrent_creation(job_store):
    """同一请求即使改变正文并行重放，也只创建原任务。"""
    results = await asyncio.gather(
        *(job_store.create(scope(), create(prompt=f"请求{i}")) for i in range(8))
    )
    assert len({item.id for item in results}) == 1
    assert len(await job_store.list_owned(scope())) == 1
    assert results[0].status == "PENDING"
    assert "system_prompt" not in results[0].model_dump()


async def test_absolute_schedule_replay_after_due_returns_original(job_store, job_clock):
    """重放旧请求不会因原绝对时间已经过去而丢失幂等结果。"""
    command = create(delaySeconds=None, runAt="2026-10-12T12:01:00Z")
    original = await job_store.create(scope(), command)
    job_clock[0] += timedelta(minutes=2)
    assert (await job_store.create(scope(), command)).id == original.id


async def test_active_limit_and_cancelled_capacity(job_store):
    """第 50 项并发竞争不会突破配额；取消后释放一个名额。"""
    for i in range(49):
        await job_store.create(scope(f"r{i}"), create())
    values = await asyncio.gather(
        job_store.create(scope("a"), create()),
        job_store.create(scope("b"), create()),
        return_exceptions=True,
    )
    assert sum(isinstance(value, AiError) for value in values) == 1
    assert next(value for value in values if isinstance(value, AiError)).code == "JOB_ACTIVE_LIMIT"
    latest = await job_store.list_owned(scope())
    assert len(latest) == 20
    await job_store.cancel(scope(), latest[0].id)
    assert (await job_store.create(scope("new"), create())).status == "PENDING"


async def test_due_claim_cancel_late_finish_and_restart(job_store, job_clock):
    """只有一个执行者，取消不可覆盖，租约过期后以新执行身份恢复。"""
    first = await job_store.create(scope(), create())
    assert await job_store.claim_due() is None
    job_clock[0] += timedelta(seconds=1)
    claims = await asyncio.gather(job_store.claim_due(), job_store.claim_due())
    assert sum(value is not None for value in claims) == 1
    assert next(value for value in claims if value).model == "offline-model"
    await job_store.cancel(scope(), first.id)
    await job_store.finish(
        first.id, lease_token=next(value for value in claims if value).lease_token,
        result="迟到结果",
    )
    assert (await job_store.get(scope(), first.id)).status == "CANCELLED"
    second = await job_store.create(scope("second"), create())
    job_clock[0] += timedelta(seconds=1)
    original = await job_store.claim_due()
    assert original.id == second.id
    job_clock[0] += timedelta(minutes=7)
    recovered = await job_store.claim_due()
    assert recovered.id == second.id and recovered.lease_token != original.lease_token
    assert not await job_store.finish(second.id, lease_token=original.lease_token, result="旧结果")
    assert await job_store.finish(second.id, lease_token=recovered.lease_token, result="新结果")
    assert (await job_store.get(scope(), second.id)).result == "新结果"


async def test_scope_time_summary_results_and_ack(job_store, job_clock):
    """结果分页、摘要、跨所有者拒绝及机器人确认均遵守原契约。"""
    with pytest.raises(AiError, match="JOB_TIME_INVALID"):
        await job_store.create(scope(), create(delaySeconds=None, runAt="2026-10-13T10:00:00"))
    with pytest.raises(AiError, match="JOB_TIME_INVALID"):
        await job_store.create(scope(), create(delaySeconds=None, runAt="2026-10-11T10:00:00Z"))
    for i in range(12):
        await job_store.create(scope(f"request-{i}"), create(prompt="字" * 500))
    assert all(
        len(item.prompt) == 256 and item.result is None
        for item in await job_store.list_owned(scope())
    )
    job_clock[0] += timedelta(seconds=2)
    for _ in range(12):
        record = await job_store.claim_due()
        await job_store.finish(record.id, lease_token=record.lease_token, result="生成结果")
    page = await job_store.pending_results("10001")
    assert len(page) == 10
    assert len(await job_store.pending_results("10001", page[-1].id)) == 2
    item = page[0]
    for field in ("bot_id", "sender_id", "session_id"):
        with pytest.raises(AiError, match="JOB_NOT_FOUND"):
            await job_store.get(scope(**{field: "90009"}), item.id)
    assert await job_store.pending_results("90009") == ()
    await job_store.acknowledge("90009", item.id)
    assert len(await job_store.pending_results("10001")) == 10
    await job_store.acknowledge("10001", item.id)
    await job_store.acknowledge("10001", item.id)
    assert item.id not in {value.id for value in await job_store.pending_results("10001")}
    assert (await job_store.get(scope(), item.id)).status == "SUCCEEDED"


async def test_repeated_crashes_are_bounded_and_audited(job_store, job_clock):
    """三次执行都硬中断后停止恢复，历史保留原身份与每次租约。"""
    job = await job_store.create(scope(), create())
    job_clock[0] += timedelta(seconds=1)
    for _ in range(3):
        assert (await job_store.claim_due()).id == job.id
        job_clock[0] += timedelta(minutes=6)
    assert await job_store.claim_due() is None
    history = await job_store.inspect(job.id)
    assert history["job"]["errorCode"] == "RECOVERY_LIMIT_REACHED"
    assert history["job"]["botId"] == "10001"
    assert len(history["attempts"]) == 3
    assert {attempt["status"] for attempt in history["attempts"]} == {"INTERRUPTED"}
    assert "system_prompt" not in history["job"]
