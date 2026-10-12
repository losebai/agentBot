from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import Adapter, Bot
from nonebug import App

from agent_bot.integrations.onebot.auto_speak import AutoSpeakService
from agent_bot.models import AutoSpeakDecisionResponse
from agent_bot.services.auto_speak_decision import AutoSpeakContextBuilder
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.state import ConversationContext


class SpeakDecisionClient:
    async def decide_auto_speak(
        self,
        *,
        state: str,
        session_id: str,
        request_id: str,
    ) -> AutoSpeakDecisionResponse:
        """Always choose speak without contacting dispatch-ai."""

        return AutoSpeakDecisionResponse(
            should_speak=True,
            confidence=0.9,
            model="multilingual",
        )


class StaticContextStore:
    def context(self, *, bot_id: str, group_id: str) -> str:
        """Return deterministic group context for the offline integration test."""

        return "群聊上下文"


@pytest.mark.integration
async def test_auto_speak_submits_a_normal_group_conversation_job(
    app: App,
) -> None:
    """Laya 允许发言后只提交普通群聊队列任务，不在决策周期直接发送。"""

    jobs: list[ConversationJob] = []

    async def enqueue(job: ConversationJob) -> str:
        """记录跨过 NoneBot 连接边界的队列任务。"""

        jobs.append(job)
        return job.job_id

    async def conversation_context(_session_id: str) -> ConversationContext:
        """返回空的自动发言 session 历史。"""

        return ConversationContext(memory="", messages=())

    async with app.test_api() as ctx:
        adapter = nonebot.get_adapter(Adapter)
        ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        service = AutoSpeakService(
            job_submitter=enqueue,
            available_tools_provider=lambda: ("memory", "tts_generate"),
            conversation_context_provider=conversation_context,
            decision_client=SpeakDecisionClient(),
            context_builder=AutoSpeakContextBuilder(),
            recent_context_store=StaticContextStore(),
            bot_provider=lambda bot_id: nonebot.get_bots().get(bot_id),
            bot_id="90001",
            group_ids={"20001"},
            trigger_mode="interval",
            prompt="主动问候",
            system_prompt="你是群聊助手。",
            laya_prompt="结合上下文判断是否主动发言。",
            session_secret="test-auto-session-secret-that-is-at-least-32-characters",
            initial_delay_seconds=60,
            interval_seconds=3600,
            max_reply_chars=500,
        )

        result = await service.run_once()

    assert result.sent == 1
    assert result.failed == 0
    assert len(jobs) == 1
    assert jobs[0].scene == "group"
    assert jobs[0].group_id == "20001"
    assert jobs[0].chat_tools == ("memory", "tts_generate")


@dataclass
class BlockingAutoSpeakService:
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_started: asyncio.Event = field(default_factory=asyncio.Event)
    allow_cleanup: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_finished: asyncio.Event = field(default_factory=asyncio.Event)
    runs_on_interval: bool = True

    async def run(self) -> None:
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.cleanup_started.set()
            await self.allow_cleanup.wait()
            self.cleanup_finished.set()


@pytest.mark.integration
async def test_auto_speak_start_and_stop_are_idempotent_and_stop_awaits_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    service = BlockingAutoSpeakService()
    monkeypatch.setattr(chat_plugin, "auto_speak_service", service)
    monkeypatch.setattr(chat_plugin, "_auto_speak_task", None)

    await chat_plugin.start_auto_speak()
    await service.started.wait()
    first_task = chat_plugin._auto_speak_task

    await chat_plugin.start_auto_speak()
    assert chat_plugin._auto_speak_task is first_task
    assert first_task is not None

    stop_task = asyncio.create_task(chat_plugin.stop_auto_speak())
    await service.cleanup_started.wait()
    await asyncio.sleep(0)
    assert not stop_task.done()

    service.allow_cleanup.set()
    await stop_task

    assert service.cleanup_finished.is_set()
    assert chat_plugin._auto_speak_task is None
    await chat_plugin.stop_auto_speak()


@pytest.mark.integration
async def test_message_mode_does_not_start_interval_background_task(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    service = BlockingAutoSpeakService(runs_on_interval=False)
    monkeypatch.setattr(chat_plugin, "auto_speak_service", service)
    monkeypatch.setattr(chat_plugin, "_auto_speak_task", None)

    await chat_plugin.start_auto_speak()

    assert chat_plugin._auto_speak_task is None
    assert service.started.is_set() is False


@pytest.mark.integration
async def test_shutdown_stops_auto_speak_before_closing_clients(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    order: list[str] = []

    async def stop_auto_speak() -> None:
        order.append("stop-auto-speak")

    async def stop_settings_refresh() -> None:
        order.append("stop-settings-refresh")

    async def stop_queue() -> None:
        order.append("stop-queue")

    async def stop_image_queue() -> None:
        order.append("stop-image-queue")

    async def close_redis() -> None:
        order.append("close-redis")

    async def close_ai(_client: object) -> None:
        order.append("close-ai")

    async def close_naraka(_client: object) -> None:
        order.append("close-naraka")

    fake_ai_client = type("FakeAiClient", (), {"aclose": close_ai})()
    fake_naraka_client = type("FakeNarakaClient", (), {"aclose": close_naraka})()
    monkeypatch.setattr(chat_plugin, "stop_settings_refresh", stop_settings_refresh)
    monkeypatch.setattr(chat_plugin, "stop_auto_speak", stop_auto_speak)
    monkeypatch.setattr(chat_plugin.conversation_queue, "stop", stop_queue)
    monkeypatch.setattr(chat_plugin.image_queue, "stop", stop_image_queue)
    monkeypatch.setattr(chat_plugin._redis_client, "aclose", close_redis)
    monkeypatch.setattr(
        chat_plugin,
        "_runtime_state",
        replace(
            chat_plugin._runtime_state,
            ai_client=fake_ai_client,
            naraka_client=fake_naraka_client,
        ),
    )

    await chat_plugin.close_clients()

    assert order == [
        "stop-queue",
        "stop-image-queue",
        "close-redis",
        "stop-settings-refresh",
        "stop-auto-speak",
        "close-ai",
        "close-naraka",
    ]
