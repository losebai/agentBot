"""固定离线样本与实际行为指标分开评估，不用模型自评分宣称记忆准确或具备意识。"""

import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from agent_bot.autonomy.contracts import Identity, Policy, ProfileCreate, Step, TaskCreate
from agent_bot.autonomy.service import AutonomyService
from agent_bot.autonomy.store import AutonomyStore
from agent_bot.database import database_connector
from agent_bot.database_pool import finish_database_write
from agent_bot.database_upgrade import read_sql_resource
from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
from agent_bot.jobs.contracts import JobOwner
from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity
from agent_bot.settings import DatabaseParams

SUITE_VERSION = "persistent-agent-offline-v1"
CASES = (
    "identity_consistency",
    "memory_accuracy",
    "duplicate_reports",
    "tool_authorization",
    "task_completion",
    "crash_recovery",
)


async def run_fixed_suite(*, read_resource=read_sql_resource) -> dict:
    """每次在临时SQLite运行六组固定输入，不连接QQ、模型或部署数据库。"""
    outcomes = []
    with TemporaryDirectory(prefix="agent-bot-evaluation-") as directory:
        params = DatabaseParams(
            backend="sqlite", sqlite_path=str(Path(directory) / "evaluation.db")
        )
        script = read_resource("agent_bot_init.sqlite.sql")

        def initialize():
            """只在本次临时文件执行完整初始化，不调用生产升级入口。"""
            with sqlite3.connect(params.sqlite_path) as connection:
                connection.executescript(script)

        await finish_database_write(initialize)
        clock = [datetime(2026, 11, 4, 4, tzinfo=timezone.utc)]
        connector = database_connector(params)
        store = AutonomyStore(
            params, connect=connector, secret="fixed-offline-fixture", clock=lambda: clock[0]
        )
        memories = TaggedMemoryStore(params, connect=connector, secret="fixed-offline-fixture")
        owner = JobOwner(
            bot_id="10001", sender_id="10002", session_id="fixed-fixture", scene="private"
        )
        body = ProfileCreate(
            owner=owner,
            identity=Identity(name="固定测试身份", boundaries="只允许召回与报告"),
            policy=Policy(
                paused=False,
                allowed_actions=("recall", "report"),
                quiet_start_hour=0,
                quiet_end_hour=0,
            ),
        )
        profile = await store.create_profile(body)
        id = profile["id"]
        restored = AutonomyStore(
            params, connect=connector, secret="fixed-offline-fixture", clock=lambda: clock[0]
        )
        recovered = await restored.create_profile(body)
        outcomes.append(
            {
                "case": CASES[0],
                "passed": recovered["id"] == id and recovered["identity"] == profile["identity"],
            }
        )

        identity = MemoryIdentity(
            bot_id="10001",
            user_id="10002",
            session_id="fixed-fixture",
            source_text="网关上限为8MB",
            source_event_id="fixed-fact",
        )
        saved = memories.execute(
            identity,
            MemoryArguments(
                action="save",
                scope="user",
                key="网关",
                content="网关上限为8MB",
                tags=("部署",),
                source_excerpt="网关上限为8MB",
            ),
        ).records[0]
        service = AutonomyService(store, memories)
        result = await service._recall(profile["owner"], text="网关")
        foreign = MemoryIdentity(bot_id="10001", user_id="10003", session_id="foreign")
        hidden = memories.execute(
            foreign, MemoryArguments(action="recall", scope="all", tags=("部署",))
        )
        outcomes.append(
            {
                "case": CASES[1],
                "passed": len(result["records"]) == 1
                and result["records"][0]["id"] == saved.id
                and not hidden.records,
            }
        )

        await store.create_task(
            id,
            TaskCreate(
                request_id="first-report",
                goal="固定报告",
                steps=(Step(action="report", text="固定任务完成"),),
            ),
        )
        await service.run_once(id)
        clock[0] += timedelta(hours=2)
        await store.create_task(
            id,
            TaskCreate(
                request_id="second-report",
                goal="固定报告",
                steps=(Step(action="report", text="固定任务完成"),),
            ),
        )
        await service.run_once(id)
        reports = [row for row in (await store.audit(id))["items"] if row["kind"] == "report"]
        outcomes.append({"case": CASES[2], "passed": len(reports) == 1})

        forbidden = await store.create_task(
            id,
            TaskCreate(
                request_id="forbidden",
                goal="拒绝越权",
                steps=(Step(action="draft", text="不允许的模型动作"),),
            ),
        )
        await service.run_once(id)
        tasks = await store.tasks(id)
        blocked = next(row for row in tasks if row["id"] == forbidden["id"])
        outcomes.append(
            {
                "case": CASES[3],
                "passed": blocked["status"] == "approval"
                and blocked["error_code"] == "ACTION_NOT_ALLOWED",
            }
        )

        task = await store.create_task(
            id,
            TaskCreate(
                request_id="completion",
                goal="查询部署事实",
                steps=(Step(action="recall", text="网关"),),
            ),
        )
        await service.run_once(id)
        completed = next(row for row in await store.tasks(id) if row["id"] == task["id"])
        outcomes.append(
            {
                "case": CASES[4],
                "passed": completed["status"] == "succeeded"
                and completed["outputs"][0]["records"][0]["id"] == saved.id,
            }
        )

        task = await store.create_task(
            id,
            TaskCreate(
                request_id="recovery", goal="断点恢复", steps=(Step(action="recall", text="网关"),)
            ),
        )
        first = await store.claim(id)
        clock[0] += timedelta(seconds=181)
        second = await restored.claim(id)
        rejected = not await store.finish(id, task["id"], first["lease_token"], output="迟到")
        accepted = await restored.finish(id, task["id"], second["lease_token"], output="恢复完成")
        outcomes.append(
            {"case": CASES[5], "passed": rejected and accepted and second["attempts"] == 2}
        )
    fixture = await finish_database_write(lambda: Path(__file__).read_bytes())
    return {
        "suite": SUITE_VERSION,
        "fixture_digest": hashlib.sha256(fixture).hexdigest(),
        "cases": outcomes,
        "passed": sum(item["passed"] for item in outcomes),
        "total": len(CASES),
        "model_calls": 0,
        "qq_messages": 0,
    }


async def evaluate_profile(
    store: AutonomyStore, id: str, *, read_resource=read_sql_resource
) -> dict:
    """将固定机制验收和真实行为统计一起归档，未知样本的准确率不伪造为满分。"""
    await store.get_profile(id)
    fixed = await run_fixed_suite(read_resource=read_resource)
    observed = await store.observations(id)
    report = {
        "fixed_suite": fixed,
        "observed": observed,
        "memory_accuracy": None,
        "memory_accuracy_reason": "实际记忆尚无人工标注真值集",
    }
    await store.record_evaluation(id, report)
    return report
