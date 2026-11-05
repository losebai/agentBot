"""固定验收集在真实临时库执行，机制评分与生产记忆准确率明确分开。"""

from agent_bot.autonomy.evaluation import evaluate_profile
from conftest import ROOT, profile_body


async def test_fixed_suite_passes_and_report_is_persisted_without_changing_identity(store):
    """验收六项固定场景，归档结果后持续身份与真实业务任务保持原有状态。"""
    profile = await store.create_profile(profile_body())
    report = await evaluate_profile(
        store, profile["id"], read_resource=lambda name: (ROOT / "sql" / name).read_text("utf-8")
    )
    assert report["fixed_suite"]["passed"] == report["fixed_suite"]["total"] == 6
    assert report["fixed_suite"]["model_calls"] == report["fixed_suite"]["qq_messages"] == 0
    assert report["memory_accuracy"] is None
    assert report["observed"]["completion_rate"] is None
    assert (await store.get_profile(profile["id"]))["identity"] == profile["identity"]
    assert len((await store.audit(profile["id"]))["items"]) == 2
    assert await store.tasks(profile["id"]) == []
