"""四阶段固定离线验收：身份、记忆、租约、暂停、预算、计划及复盘审计。"""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.ai.errors import AiError
from agent_bot.autonomy.contracts import (
    EpisodeCreate,
    Identity,
    Policy,
    ProfileUpdate,
    Step,
    TaskCreate,
)
from agent_bot.autonomy.service import AutonomyService
from agent_bot.autonomy.store import AutonomyStore
from agent_bot.jobs.contracts import JobOwner
from agent_bot.jobs.store import JobStore
from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity
from conftest import profile_body


async def test_identity_survives_restart_with_revision_history_and_namespace_boundary(store):
    """重开连接保持身份ID；更正设定保留历史，旧密钥命名空间不可重新认领。"""
    first = await store.create_profile(profile_body())
    restored = AutonomyStore(store.params, connect=store.connect, secret="offline-autonomy-secret")
    assert (await restored.create_profile(profile_body()))["id"] == first["id"]
    updated = await restored.update_profile(
        first["id"],
        ProfileUpdate(expected_revision=1, identity=Identity(name="阿澄", role="长期助手")),
    )
    assert updated["revision"] == 2 and updated["id"] == first["id"]
    with pytest.raises(AiError, match="AUTONOMY_CONFLICT"):
        await store.update_profile(first["id"], ProfileUpdate(expected_revision=1, policy=Policy()))
    foreign = AutonomyStore(store.params, connect=store.connect, secret="other-key")
    assert await foreign.profiles() == []
    with pytest.raises(AiError, match="AUTONOMY_NOT_FOUND"):
        await foreign.get_profile(first["id"])
    audit = await store.audit(first["id"])
    assert {row["kind"] for row in audit["items"]} == {"identity_created", "identity_updated"}


async def test_step_checkpoint_recovery_fences_old_lease_and_cancellation(store, clock):
    """重启恢复只重跑未提交步骤，旧令牌和已取消任务均不能接受迟到结果。"""
    profile = await store.create_profile(
        profile_body(
            paused=False,
            allowed_actions=("draft",),
            model="model:1",
            input_cost_per_million=1,
            output_cost_per_million=1,
            daily_cost_limit=1,
        )
    )
    id = profile["id"]
    task = await store.create_task(
        id,
        TaskCreate(
            request_id="recovery",
            goal="恢复计划",
            steps=(Step(action="draft"), Step(action="draft")),
        ),
    )
    first = await store.claim(id)
    assert await store.claim(id) is None
    assert await store.finish(id, task["id"], first["lease_token"], output="步骤一")
    second = await store.claim(id)
    assert second["cursor"] == 1 and second["outputs"] == ["步骤一"]
    clock[0] += timedelta(seconds=181)
    restored = await store.claim(id)
    assert restored["cursor"] == 1 and restored["attempts"] == 2
    assert not await store.finish(id, task["id"], second["lease_token"], output="迟到")
    await store.decide_task(id, task["id"], restored["revision"], False)
    assert not await store.finish(id, task["id"], restored["lease_token"], output="不能复活")
    assert (await store.tasks(id))[0]["status"] == "cancelled"


async def test_concurrent_workers_only_one_claim_and_idle_never_infers(store, memories):
    """并发只有一个执行权，空闲和暂停不调用模型。"""
    profile = await store.create_profile(profile_body(paused=False, allowed_actions=("recall",)))
    id = profile["id"]
    ai = SimpleNamespace(complete=AsyncMock())
    service = AutonomyService(store, memories, ai)
    assert not await service.run_once(id)
    await store.create_task(
        id,
        TaskCreate(request_id="one", goal="回忆部署", steps=(Step(action="recall", text="部署"),)),
    )
    claims = await asyncio.gather(store.claim(id), store.claim(id))
    assert sum(value is not None for value in claims) == 1
    await store.pause(id)
    assert not await service.run_once(id)
    ai.complete.assert_not_awaited()


async def test_pause_permission_budget_and_quiet_hours_are_host_enforced(store, clock):
    """宿主策略独立于提示词检查；缺权限、费用和静默约束都不能被计划绕过。"""
    profile = await store.create_profile(
        profile_body(
            paused=False,
            allowed_actions=("draft",),
            model="model:1",
            input_cost_per_million=1,
            output_cost_per_million=1,
            daily_cost_limit=0.04,
        )
    )
    id = profile["id"]
    await store.create_task(
        id,
        TaskCreate(
            request_id="budget", goal="预算验证", steps=(Step(action="draft"), Step(action="draft"))
        ),
    )
    first = await store.claim(id)
    await store.finish(id, first["id"], first["lease_token"], output="一次调用")
    assert await store.claim(id) is None
    assert (await store.tasks(id))[0]["error_code"] == "DAILY_MODEL_BUDGET"
    await store.create_task(
        id,
        TaskCreate(
            request_id="forbidden", goal="禁止越权", steps=(Step(action="report", text="越权报告"),)
        ),
    )
    assert await store.claim(id) is None
    forbidden = next(row for row in await store.tasks(id) if row["goal"] == "禁止越权")
    assert forbidden["status"] == "approval" and forbidden["error_code"] == "ACTION_NOT_ALLOWED"
    await store.decide_task(id, forbidden["id"], forbidden["revision"], True)
    assert await store.claim(id) is None  # 人工审批也不扩大白名单。
    clock[0] += timedelta(days=1)
    assert (await store.claim(id))["step"]["action"] == "draft"


async def test_plan_execution_and_report_approval_are_persistent(store, memories):
    """计划和对话结果持久化；QQ报告绑定审批，入队与实际投递分别确认。"""
    profile = await store.create_profile(
        profile_body(
            paused=False,
            allowed_actions=("plan", "draft", "report"),
            notify_qq=True,
            model="model:1",
            input_cost_per_million=1,
            output_cost_per_million=1,
            daily_cost_limit=1,
        )
    )
    id = profile["id"]
    ai = SimpleNamespace(
        complete=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    content=json.dumps(
                        {
                            "steps": [
                                {"action": "draft", "text": "整理"},
                                {"action": "report", "text": "任务已整理完成"},
                            ]
                        }
                    )
                ),
                SimpleNamespace(content="整理结果"),
            ]
        )
    )
    service = AutonomyService(store, memories, ai)
    await store.create_task(id, TaskCreate(request_id="plan", goal="整理部署计划"))
    assert await service.run_once(id)
    assert await service.run_once(id)
    assert not await service.run_once(id)
    task = (await store.tasks(id))[0]
    assert task["revision"] == 2 and task["status"] == "approval"
    await store.decide_task(id, task["id"], 2, True)
    await service.run_once(id)
    task = (await store.tasks(id))[0]
    assert task["status"] == "succeeded", task["error_code"]
    report_id = task["outputs"][-1]["report_id"]
    conversation = (await store.conversations(id))['items'][0]['task']
    assert conversation['outputs'][0] == '整理结果'
    assert conversation['outputs'][-1]['delivery'] == 'queued'
    jobs = JobStore(store.params, connect=store.connect, clock=store.clock)
    await jobs.acknowledge('10001', report_id)
    conversation = (await store.conversations(id))['items'][0]['task']
    assert conversation['outputs'][-1]['delivery'] == 'delivered'
    # 页面状态只富化读取副本，不写回实际执行检查点。
    assert 'delivery' not in (await store.tasks(id))[0]['outputs'][-1]
    assert await store.delivery_allowed(report_id)
    await store.pause(id)
    assert not await store.delivery_allowed(report_id)
    assert ai.complete.await_count == 2
    assert "记忆助手" in ai.complete.await_args_list[0].args[0].messages[0].content


async def test_discovery_consolidation_correction_and_forgetting(
    store, memories, clock, monkeypatch
):
    """事件自动发现任务，事实更正带版本和原始证据；情节过期不删除已确认事实。"""
    monkeypatch.setattr("agent_bot.integrations.memory_lifecycle.utc_now", store.now)
    profile = await store.create_profile(
        profile_body(
            paused=False,
            allowed_actions=("consolidate", "recall"),
            auto_consolidate=True,
            auto_correct=True,
            model="model:1",
            input_cost_per_million=1,
            output_cost_per_million=1,
            daily_cost_limit=1,
        )
    )
    id = profile["id"]
    source = MemoryIdentity(
        bot_id="10001",
        user_id="10002",
        session_id="offline-session",
        source_text="我喜欢茶",
        source_event_id="initial",
    )
    original = memories.execute(
        source,
        MemoryArguments(
            action="save",
            scope="user",
            key="偏好",
            content="我喜欢茶",
            tags=("饮品",),
            kind="preference",
            source_excerpt="我喜欢茶",
        ),
    ).records[0]
    await store.ingest(
        id, EpisodeCreate(event_id="change", text="更正：我现在喜欢咖啡", occurred_at=clock[0])
    )
    proposal = {
        "summary": "饮品偏好已更正",
        "facts": [
            {
                "key": "偏好",
                "content": "我现在喜欢咖啡",
                "excerpt": "我现在喜欢咖啡",
                "kind": "preference",
                "tags": ["饮品"],
                "correction_id": original.id,
                "expected_revision": 1,
            }
        ],
    }
    ai = SimpleNamespace(
        complete=AsyncMock(return_value=SimpleNamespace(content=json.dumps(proposal)))
    )
    service = AutonomyService(store, memories, ai)
    await service.run_once(id)
    await service.run_once(id)
    changed = memories.admin_get(original.id)
    assert changed.content == "我现在喜欢咖啡" and changed.revision == 2
    assert (await store.proposals(id))[0]["status"] == "applied"
    assert (await service.memory_view(id, query="咖啡"))["facts"][0]["id"] == original.id
    clock[0] += timedelta(days=91)
    await service.run_once(id)
    assert await store.episodes(id) == []
    assert memories.admin_get(original.id).content == "我现在喜欢咖啡"
    assert any(row["kind"] == "episode_forgotten" for row in (await store.audit(id))["items"])


async def test_unquoted_fact_never_reaches_long_term_memory(store, memories, clock):
    """模型编造来源时整个提取失败，不能把摘要或猜测当作用户记忆。"""
    profile = await store.create_profile(
        profile_body(
            paused=False,
            auto_consolidate=True,
            allowed_actions=("consolidate",),
            model="model:1",
            input_cost_per_million=1,
            output_cost_per_million=1,
            daily_cost_limit=1,
        )
    )
    id = profile["id"]
    await store.ingest(
        id, EpisodeCreate(event_id="ordinary", text="今天天气很好", occurred_at=clock[0])
    )
    ai = SimpleNamespace(
        complete=AsyncMock(
            return_value=SimpleNamespace(
                content=json.dumps(
                    {
                        "summary": "天气",
                        "facts": [
                            {
                                "key": "关系",
                                "content": "用户是管理员",
                                "excerpt": "虚构片段",
                                "tags": ["权限"],
                                "kind": "relationship",
                            }
                        ],
                    }
                )
            )
        )
    )
    await AutonomyService(store, memories, ai).run_once(id)
    assert await store.proposals(id) == []
    assert (await store.tasks(id))[0]["error_code"] == "AUTONOMY_UNGROUNDED_MEMORY"


async def test_event_discovery_is_opt_in_idempotent_and_third_party_cannot_request_tasks(
    store, clock
):
    """只消费已授权来源的任务意图；同事件重放不重复创建任务。"""
    profile = await store.create_profile(
        profile_body(paused=False, discover_tasks=True, allowed_actions=("recall",))
    )
    id = profile["id"]
    event = EpisodeCreate(
        event_id="request", text="整理一下部署资料", occurred_at=clock[0], task_requested=True
    )
    assert await store.ingest(id, event) == await store.ingest(id, event)
    await store.discover(id)
    clock[0] += timedelta(seconds=61)
    await store.discover(id)
    assert len(await store.tasks(id)) == 1
    with pytest.raises(ValueError, match="third party"):
        EpisodeCreate(
            event_id="bad",
            text="代替用户发起任务",
            occurred_at=clock[0],
            source="third_party",
            task_requested=True,
        )


async def test_retry_limit_stops_failed_steps_and_exposes_errors_in_self_state(store, clock):
    """失败三次终止恢复并保留错误状态，单独的身份设定不会被失败内容改写。"""
    profile = await store.create_profile(profile_body(paused=False, allowed_actions=("recall",)))
    id = profile["id"]
    await store.create_task(
        id, TaskCreate(request_id="fail", goal="失败恢复", steps=(Step(action="recall"),))
    )
    for _ in range(3):
        claim = await store.claim(id)
        assert claim is not None
        await store.finish(id, claim["id"], claim["lease_token"], error="ACTION_EXECUTION_FAILED")
        clock[0] += timedelta(seconds=301)
    assert await store.claim(id) is None
    assert (await store.tasks(id))[0]["status"] == "failed"
    current = await store.get_profile(id)
    assert current["state"]["recent_errors"] == ["ACTION_EXECUTION_FAILED"] * 3
    assert current["identity"] == profile["identity"]


async def test_quiet_hours_hold_report_until_daytime_and_due_time_survives_restart(store, clock):
    """定时与跨午夜静默分别限制领取，重建执行器不会提前发送。"""
    profile = await store.create_profile(
        profile_body(
            paused=False, allowed_actions=("report",), quiet_start_hour=23, quiet_end_hour=8
        )
    )
    id = profile["id"]
    due = clock[0] + timedelta(hours=12)
    await store.create_task(
        id,
        TaskCreate(
            request_id="night",
            goal="按时汇报",
            steps=(Step(action="report", text="报告"),),
            run_at=due,
        ),
    )
    assert await store.claim(id) is None
    clock[0] = due
    restored = AutonomyStore(
        store.params,
        connect=store.connect,
        secret="offline-autonomy-secret",
        clock=lambda: clock[0],
    )
    assert await restored.claim(id) is None
    assert (await store.tasks(id))[0]["error_code"] == "QUIET_HOURS"
    clock[0] += timedelta(hours=8)
    assert (await restored.claim(id))["step"]["action"] == "report"


async def test_running_approval_is_rejected_and_policy_change_requires_reapproval(store):
    """审批不能夺取在途租约；策略变化使旧批准失效，QQ出站沿用原任务所有权。"""
    profile = await store.create_profile(
        profile_body(paused=False, allowed_actions=("recall", "report"), notify_qq=True)
    )
    id = profile["id"]
    await store.create_task(
        id,
        TaskCreate(
            request_id="policy",
            goal="核对后汇报",
            steps=(Step(action="recall"), Step(action="report", text="已核对")),
        ),
    )
    claim = await store.claim(id)
    with pytest.raises(AiError, match="AUTONOMY_CONFLICT"):
        await store.decide_task(id, claim["id"], claim["revision"], True)
    assert await store.finish(id, claim["id"], claim["lease_token"], output="核对完成")
    assert await store.claim(id) is None
    await store.decide_task(id, claim["id"], 1, True)
    changed = Policy.model_validate({**profile["policy"], "max_reports_per_day": 2})
    await store.update_profile(id, ProfileUpdate(expected_revision=1, policy=changed))
    assert await store.claim(id) is None
    assert (await store.tasks(id))[0]["error_code"] == "REPORT_APPROVAL_REQUIRED"
    await store.decide_task(id, claim["id"], 1, True)
    report = await store.claim(id)
    receipt = await store.publish_report(report, "已核对")
    repeated = await store.publish_report(report, "已核对")
    assert repeated == receipt
    jobs = JobStore(store.params, connect=store.connect)
    view = await jobs.get(JobOwner.model_validate(profile["owner"]), receipt["report_id"])
    assert view.autonomy_profile_id == id
    assert view.model_dump(by_alias=True)["autonomyProfileId"] == id
    assert await store.finish(id, report["id"], report["lease_token"], output=receipt)
    assert await store.delivery_allowed(receipt["report_id"])


async def test_late_opt_in_revisits_existing_events_without_duplicate_tasks(store, clock):
    """先归档后授权的事件仍能发现任务，重启和重复扫描不创建第二份计划。"""
    profile = await store.create_profile(profile_body(paused=False, allowed_actions=("recall",)))
    id = profile["id"]
    await store.ingest(
        id,
        EpisodeCreate(
            event_id="before-enable", text="整理资料", occurred_at=clock[0], task_requested=True
        ),
    )
    await store.discover(id)
    assert await store.tasks(id) == []
    policy = Policy.model_validate({**profile["policy"], "discover_tasks": True})
    await store.update_profile(id, ProfileUpdate(expected_revision=1, policy=policy))
    await store.discover(id)
    assert len(await store.tasks(id)) == 1
    clock[0] += timedelta(seconds=61)
    await store.discover(id)
    assert len(await store.tasks(id)) == 1


async def test_daily_review_runs_fixed_suite_once_and_persists_observations(store, memories, clock):
    """每天只创建一份复盘，固定机制得分和本身份的真实任务统计分别归档。"""
    from conftest import ROOT

    profile = await store.create_profile(profile_body(paused=False, allowed_actions=("review",)))
    service = AutonomyService(
        store, memories, read_resource=lambda name: (ROOT / "sql" / name).read_text("utf-8")
    )
    assert await service.run_once(profile["id"])
    clock[0] += timedelta(seconds=61)
    assert not await service.run_once(profile["id"])
    tasks = await store.tasks(profile["id"])
    assert len(tasks) == 1 and tasks[0]["status"] == "succeeded"
    result = tasks[0]["outputs"][0]
    assert result["fixed_suite"]["passed"] == 6
    assert result["fixed_suite"]["model_calls"] == 0
    assert any(row["kind"] == "evaluation" for row in (await store.audit(profile["id"]))["items"])


async def test_report_commit_recovers_without_waiting_for_daily_rate_limit(store, clock):
    """出站已提交但步骤未完成时，恢复复用回执，不因当天限额耗尽永久等待。"""
    profile = await store.create_profile(
        profile_body(paused=False, allowed_actions=("report",), max_reports_per_day=1)
    )
    id = profile["id"]
    await store.create_task(
        id,
        TaskCreate(
            request_id="report-crash", goal="汇报", steps=(Step(action="report", text="报告"),)
        ),
    )
    first = await store.claim(id)
    receipt = await store.publish_report(first, "报告")
    clock[0] += timedelta(seconds=181)
    resumed = await store.claim(id)
    assert resumed is not None and resumed["attempts"] == 2
    assert await store.publish_report(resumed, "报告") == receipt
    assert await store.finish(id, resumed["id"], resumed["lease_token"], output=receipt)
    assert (await store.get_profile(id))["usage"]["reports"] == 1


async def test_terminal_failure_creates_one_report_that_still_requires_approval(store):
    """失败主动汇报持久化且需要审核，恢复或重复提交不能生成第二份通知。"""
    profile = await store.create_profile(
        profile_body(
            paused=False, allowed_actions=("recall", "report"), notify_qq=True, max_attempts=1
        )
    )
    id = profile["id"]
    await store.create_task(
        id, TaskCreate(request_id="failure-notice", goal="核对资料", steps=(Step(action="recall"),))
    )
    claim = await store.claim(id)
    assert await store.finish(id, claim["id"], claim["lease_token"], error="OFFLINE_TEST_FAILURE")
    assert not await store.finish(
        id, claim["id"], claim["lease_token"], error="OFFLINE_TEST_FAILURE"
    )
    assert await store.claim(id) is None
    tasks = await store.tasks(id)
    assert len(tasks) == 2
    report = next(task for task in tasks if task["id"] != claim["id"])
    assert report["status"] == "approval"
    assert "OFFLINE_TEST_FAILURE" in report["steps"][0]["text"]


async def test_semantic_duplicate_waits_for_review_and_merges_sources(
    store, memories, clock, monkeypatch
):
    """非逐字相同的语义候选先审核，批准后确认原事实而非增加互相冲突的新事实。"""
    profile = await store.create_profile(
        profile_body(
            paused=False,
            auto_consolidate=True,
            allowed_actions=("consolidate",),
            model="model:1",
            input_cost_per_million=1,
            output_cost_per_million=1,
            daily_cost_limit=1,
        )
    )
    id = profile["id"]
    original = memories.execute(
        MemoryIdentity(
            bot_id="10001",
            user_id="10002",
            session_id="offline-session",
            source_text="网关上限8MB",
            source_event_id="first",
        ),
        MemoryArguments(
            action="save",
            scope="user",
            key="网关上限",
            content="网关上限8MB",
            tags=("部署",),
            source_excerpt="网关上限8MB",
        ),
    ).records[0]
    await store.ingest(
        id, EpisodeCreate(event_id="similar", text="网关最多允许8MB", occurred_at=clock[0])
    )
    result = {
        "summary": "再次确认网关限制",
        "facts": [
            {
                "key": "限制",
                "content": "网关最多允许8MB",
                "excerpt": "网关最多允许8MB",
                "tags": ["部署"],
                "duplicate_id": original.id,
                "duplicate_revision": original.revision,
            }
        ],
    }
    ai = SimpleNamespace(
        complete=AsyncMock(return_value=SimpleNamespace(content=json.dumps(result)))
    )
    service = AutonomyService(store, memories, ai)
    await service.run_once(id)
    await service.run_once(id)
    proposal = (await store.proposals(id))[0]
    assert proposal["status"] == "review"
    assert memories.admin_get(original.id).revision == original.revision
    await store.decide_proposal(id, proposal["id"], proposal["revision"], True)
    complete = store.complete_proposal
    monkeypatch.setattr(
        store, "complete_proposal", AsyncMock(side_effect=RuntimeError("interrupted"))
    )
    with pytest.raises(RuntimeError, match="interrupted"):
        await service.apply_proposals(id)
    committed_revision = memories.admin_get(original.id).revision
    monkeypatch.setattr(store, "complete_proposal", complete)
    await service.apply_proposals(id)
    applied = (await store.proposals(id))[0]
    assert applied["status"] == "applied", applied["document"]["result"]
    record = memories.admin_get(original.id)
    assert record.content == original.content
    assert record.revision == committed_revision
    assert applied["document"]["result"]["records"][0]["id"] == original.id


async def test_memory_task_creation_failure_leaves_receipt_retryable(
    store, memories, clock, monkeypatch
):
    """事实成功后任务创建失败时，提案保持可恢复；重试复用记忆回执并只创建一个任务。"""
    profile = await store.create_profile(
        profile_body(
            paused=False,
            auto_consolidate=True,
            discover_tasks=True,
            allowed_actions=("consolidate",),
            model="model:1",
            input_cost_per_million=1,
            output_cost_per_million=1,
            daily_cost_limit=1,
        )
    )
    id = profile["id"]
    await store.ingest(
        id, EpisodeCreate(event_id="task-memory", text="请整理部署资料", occurred_at=clock[0])
    )
    result = {
        "summary": "用户请求整理资料",
        "facts": [
            {
                "key": "整理部署",
                "content": "请整理部署资料",
                "excerpt": "请整理部署资料",
                "kind": "task",
                "tags": ["部署"],
            }
        ],
    }
    service = AutonomyService(
        store,
        memories,
        SimpleNamespace(
            complete=AsyncMock(return_value=SimpleNamespace(content=json.dumps(result)))
        ),
    )
    assert await service.run_once(id)
    create_task = store._create_task

    def unavailable(*args, **kwargs):
        """模拟配额已满，在完成提案同一事务中拒绝新任务。"""
        raise AiError("AUTONOMY_TASK_LIMIT", status_code=409)

    monkeypatch.setattr(store, "_create_task", unavailable)
    with pytest.raises(AiError, match="AUTONOMY_TASK_LIMIT"):
        await service.apply_proposals(id)
    assert (await store.proposals(id))[0]["status"] == "applying"
    monkeypatch.setattr(store, "_create_task", create_task)
    await service.apply_proposals(id)
    await service.apply_proposals(id)
    assert (await store.proposals(id))[0]["status"] == "applied"
    assert len([task for task in await store.tasks(id) if task["goal"] == "请整理部署资料"]) == 1
