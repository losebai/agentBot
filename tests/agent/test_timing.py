"""离线验证耗时阶段、归档等待以及失败和取消日志，不产生模型费用。"""

import asyncio
import logging
from types import SimpleNamespace

import pytest

from agent_bot.agent.contracts import ExecutionAuthority, ModelEvent, ModelTurn
from agent_bot.agent.errors import AgentError
from agent_bot.agent.records import AgentRunStore

from .test_service import request, service_for


def timings(caplog):
    """只读取结构化性能日志，避免依赖显示格式。"""
    return [record.llm_timing for record in caplog.records if hasattr(record, "llm_timing")]


async def test_timing_includes_model_start_archive_and_ignores_empty_deltas(
    agent_database, agent_params, seeded_model, caplog, monkeypatch,
):
    """首正文和尝试总耗时包括 model_start 记录等待，空增量不算首正文。"""
    now = [0.0]
    clock = SimpleNamespace(monotonic=lambda: now[0])
    monkeypatch.setattr("agent_bot.agent.service.time", clock)
    monkeypatch.setattr("agent_bot.agent.graph.time", clock)
    monkeypatch.setattr("agent_bot.services.llm_timing.monotonic", clock.monotonic)
    caplog.set_level(logging.INFO, logger="agent_bot.services.llm_timing")

    class Store(AgentRunStore):
        """人为推进归档时钟，实际记录仍落入离线数据库。"""

        async def begin(self, value):
            """准备后的初始归档占用十毫秒。"""
            now[0] += 0.01
            return await super().begin(value)

        async def append_step(self, value):
            """模型开始记录在供应商调用前占用二十毫秒。"""
            now[0] += 0.02
            return await super().append_step(value)

        async def finish(self, value):
            """最终归档占用三十毫秒。"""
            now[0] += 0.03
            return await super().finish(value)

    class Provider:
        """按确定时序返回空增量、正文和结束标记。"""

        def validate_options(self, options):
            """替身接受默认参数。"""

        async def stream_turn(self, call):
            """正文与敏感提示均不能进入性能日志。"""
            now[0] += 0.04
            yield ModelEvent(kind="text_delta", delta="")
            now[0] += 0.05
            yield ModelEvent(kind="text_delta", delta="private-response")
            now[0] += 0.06
            yield ModelEvent(kind="turn_end", turn=ModelTurn(
                model="private-upstream", content="private-response",
            ))

    service = service_for(agent_database, agent_params,
                          records=Store(agent_params, connect=agent_database.connect))
    service.providers["openai-chat"] = Provider()
    result = await service.complete(
        request().model_copy(update={"prompt": "private-prompt"}),
        ExecutionAuthority(entry="admin"),
    )
    records = timings(caplog)
    assert {item["stage"] for item in records} >= {
        "agent_prepare", "agent_record", "agent_total", "model_attempt",
    }
    attempt = next(item for item in records if item["stage"] == "model_attempt")
    assert attempt["status"] == "completed" and attempt["turn"] == 1
    assert attempt["model"] == seeded_model[2].reference
    assert attempt["first_text_ms"] == pytest.approx(110)
    assert attempt["duration_ms"] == pytest.approx(170)
    assert all(item["request_id"] == result.execution_id for item in records)
    total = next(item for item in records if item["stage"] == "agent_total")
    assert total["duration_ms"] == pytest.approx(210)
    assert "private-prompt" not in caplog.text and "private-response" not in caplog.text
    assert "private-upstream" not in caplog.text


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_and_cancelled_model_attempts_are_recorded(
    agent_database, agent_params, seeded_model, caplog, cancelled,
):
    """失败和取消也必须记录模型尝试与整个请求终态。"""
    started = asyncio.Event()
    caplog.set_level(logging.INFO, logger="agent_bot.services.llm_timing")

    class Provider:
        """只模拟受控失败，不访问任何上游。"""

        def validate_options(self, options):
            """默认参数没有差异。"""

        async def stream_turn(self, call):
            """通知取消边界，或返回不可转发的响应错误。"""
            started.set()
            if cancelled:
                await asyncio.Event().wait()
            raise AgentError("AGENT_MODEL_RESPONSE_INVALID")
            yield

    service = service_for(agent_database, agent_params)
    service.providers["openai-chat"] = Provider()
    task = asyncio.create_task(service.complete(request(), ExecutionAuthority(entry="admin")))
    await asyncio.wait_for(started.wait(), 2)
    if cancelled:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancelled else AgentError):
        await task
    records = timings(caplog)
    expected = "cancelled" if cancelled else "failed"
    assert next(item for item in records if item["stage"] == "model_attempt")["status"] == expected
    assert next(item for item in records if item["stage"] == "agent_total")["status"] == expected


async def test_prepare_failure_has_timing_without_starting_a_model(
    agent_database, agent_params, seeded_model, caplog,
):
    """准备失败不能遗漏总耗时，也不能伪造未开始的模型尝试。"""
    caplog.set_level(logging.INFO, logger="agent_bot.services.llm_timing")
    service = service_for(agent_database, agent_params)
    with pytest.raises(AgentError):
        await service.complete(request().model_copy(update={"model": "missing"}),
                               ExecutionAuthority(entry="admin"))
    records = timings(caplog)
    assert next(item for item in records if item["stage"] == "agent_prepare")["status"] == "failed"
    assert next(item for item in records if item["stage"] == "agent_total")["status"] == "failed"
    assert not any(item["stage"] == "model_attempt" for item in records)


async def test_explicit_fallback_has_separate_attempt_status_and_model_identity(
    agent_database, agent_params, seeded_model, caplog, monkeypatch,
):
    """同一轮主模型失败和显式备用成功各记一次，不能把两次网络尝试合为一条。"""
    from agent_bot.ai.errors import AiError

    service = service_for(agent_database, agent_params)
    original_snapshot = service.catalog.snapshot
    fallback_id = seeded_model[2].id + 1
    caplog.set_level(logging.INFO, logger="agent_bot.services.llm_timing")

    async def snapshot(value):
        """只在离线快照中登记备用模型，不改变已有目录配置。"""
        current = await original_snapshot(value)
        fallback = current.resolved.model_copy(update={
            "model": current.resolved.model.model_copy(update={"id": fallback_id}),
        })
        return current.model_copy(update={"fallback": fallback})

    class Provider:
        """仅主模型返回上游错误，备用正常结束。"""

        def validate_options(self, options):
            """默认参数不需要校验。"""

        async def stream_turn(self, call):
            """按模型身份区分尝试，避免以循环次数掩盖路由错误。"""
            if call.resolved.model.id != fallback_id:
                raise AiError("AI_UPSTREAM_UNAVAILABLE", status_code=502)
            yield ModelEvent(kind="text_delta", delta="备用回答")
            yield ModelEvent(kind="turn_end", turn=ModelTurn(content="备用回答", model="offline"))

    monkeypatch.setattr(service.catalog, "snapshot", snapshot)
    service.providers["openai-chat"] = Provider()
    result = await service.complete(request(), ExecutionAuthority(entry="admin"))
    attempts = [item for item in timings(caplog) if item["stage"] == "model_attempt"]
    assert result.content == "备用回答"
    assert [(item["status"], item["model"], item["turn"]) for item in attempts] == [
        ("failed", seeded_model[2].reference, 1), ("completed", f"model:{fallback_id}", 1),
    ]
    assert "first_text_ms" not in attempts[0] and attempts[1]["first_text_ms"] >= 0
