from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence

import pytest

from agent_bot.ai.chat_errors import ChatAiUnavailableError
from agent_bot.integrations.onebot.auto_speak import AutoSpeakCycleResult, AutoSpeakService
from agent_bot.models import AutoSpeakDecisionResponse, ChatMessage, ChatToolName
from agent_bot.services.auto_speak_decision import AutoSpeakContextBuilder
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.state import ConversationContext, external_session_id


class FakeJobSubmitter:
    """记录普通会话队列任务，并可模拟指定群入队失败。"""

    def __init__(self, failing_groups: set[str] | None = None) -> None:
        self.jobs: list[ConversationJob] = []
        self._failing_groups = failing_groups or set()

    async def __call__(self, job: ConversationJob) -> str:
        """保存任务；配置的群以脱敏异常模拟队列失败。"""

        if job.group_id in self._failing_groups:
            raise RuntimeError("queue rejected auto-speak job")
        self.jobs.append(job)
        return job.job_id


class FakeBot:
    def __init__(self, *, self_id: str = "90009", failing_groups: set[int] | None = None) -> None:
        self.self_id = self_id
        self.calls: list[tuple[str, dict[str, object]]] = []
        self._failing_groups = failing_groups or set()

    async def call_api(self, api: str, **data: object) -> dict[str, int]:
        self.calls.append((api, data))
        group_id = int(data.get("group_id", 0))
        if group_id in self._failing_groups:
            raise RuntimeError(f"QQ rejected sensitive target {group_id}")
        return {"message_id": len(self.calls)}

    async def send_group_msg(self, **data: object) -> dict[str, int]:
        """Route the concrete gateway API into the offline delivery recorder."""

        return await self.call_api("send_group_msg", **data)


class FakeDecisionClient:
    def __init__(self, outcomes: list[AutoSpeakDecisionResponse | Exception]) -> None:
        self.outcomes = outcomes
        self.calls: list[dict[str, str]] = []

    async def decide_auto_speak(
        self,
        *,
        state: str,
        session_id: str,
        request_id: str,
    ) -> AutoSpeakDecisionResponse:
        self.calls.append(
            {"state": state, "session_id": session_id, "request_id": request_id}
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeContextStore:
    def __init__(self, contexts: dict[str, str | None]) -> None:
        self.contexts = contexts
        self.calls: list[tuple[str, str]] = []

    def context(self, *, bot_id: str, group_id: str) -> str | None:
        self.calls.append((bot_id, group_id))
        return self.contexts[group_id]


class FakeConversationContextProvider:
    """返回自动发言 session 的持久历史并记录读取范围。"""

    def __init__(self, context: ConversationContext | None = None) -> None:
        self.context = context or ConversationContext(memory="", messages=())
        self.calls: list[str] = []

    async def __call__(self, session_id: str) -> ConversationContext:
        """读取指定自动发言 session 的上下文。"""

        self.calls.append(session_id)
        return self.context


def decision(should_speak: bool, confidence: float = 0.8) -> AutoSpeakDecisionResponse:
    return AutoSpeakDecisionResponse(
        should_speak=should_speak,
        confidence=confidence,
        model="multilingual",
    )


def make_service(
    *,
    job_submitter: FakeJobSubmitter,
    decision_client: FakeDecisionClient,
    context_store: FakeContextStore,
    bot_provider: Callable[[str], FakeBot | None],
    group_ids: set[str] | frozenset[str] = frozenset({"88888"}),
    trigger_mode: str = "interval",
    prompt: str = "生成一句自然的群聊开场白",
    system_prompt: str = "你是群聊助手。",
    session_secret: str = "auto-speak-test-secret",
    initial_delay_seconds: float = 10,
    interval_seconds: float = 60,
    max_reply_chars: int = 500,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    on_error: Callable[[str], None] | None = None,
    available_tools: Sequence[ChatToolName] = (),
    conversation_context_provider: FakeConversationContextProvider | None = None,
    system_prompt_provider: Callable[[], Awaitable[str | None]] | None = None,
) -> AutoSpeakService:
    """用离线依赖构造群聊自动发言，允许覆盖机器人人设。"""

    return AutoSpeakService(
        job_submitter=job_submitter,
        available_tools_provider=lambda: available_tools,
        conversation_context_provider=(
            conversation_context_provider or FakeConversationContextProvider()
        ),
        decision_client=decision_client,
        context_builder=AutoSpeakContextBuilder(),
        recent_context_store=context_store,
        bot_provider=bot_provider,
        bot_id="90009",
        group_ids=group_ids,
        trigger_mode=trigger_mode,
        prompt=prompt,
        system_prompt=system_prompt,
        system_prompt_provider=system_prompt_provider,
        laya_prompt="结合上下文判断是否应该主动发言。",
        session_secret=session_secret,
        initial_delay_seconds=initial_delay_seconds,
        interval_seconds=interval_seconds,
        max_reply_chars=max_reply_chars,
        sleep=sleep,
        on_error=on_error,
    )


def decision_payload(state: str) -> dict[str, object]:
    """Decode the context builder payload captured by the decision client."""

    payload = json.loads(state.partition("\n")[2])
    assert isinstance(payload, dict)
    return payload


@pytest.mark.asyncio
async def test_auto_speak_uses_latest_agent_prompt_and_respects_empty_role():
    """每次判定读取当前人设，明确清空不能恢复旧全局提示词。"""
    role = "当前 Agent 的人设"

    async def current_prompt():
        """模拟热更新目录的异步读取，不模拟判定上下文的构造。"""
        return role

    client = FakeDecisionClient([decision(False), decision(False)])
    service = make_service(
        job_submitter=FakeJobSubmitter(), decision_client=client,
        context_store=FakeContextStore({"88888": "最近群聊"}), bot_provider=lambda _: FakeBot(),
        system_prompt="旧全局人设", system_prompt_provider=current_prompt,
    )
    await service.run_once()
    first = decision_payload(client.calls[0]["state"])["trusted_instructions"]
    assert first["system_prompt"] == role
    role = ""
    await service.run_once()
    assert decision_payload(client.calls[1]["state"])["trusted_instructions"]["system_prompt"] == ""


@pytest.mark.asyncio
async def test_run_once_rechecks_bot_before_context_or_decision() -> None:
    decision_client = FakeDecisionClient([decision(True)])
    context_store = FakeContextStore({"88888": "群聊上下文"})
    service = make_service(
        job_submitter=FakeJobSubmitter(),
        decision_client=decision_client,
        context_store=context_store,
        bot_provider=lambda bot_id: None,
    )

    result = await service.run_once()

    assert result == AutoSpeakCycleResult(bot_available=False, sent=0, failed=0)
    assert context_store.calls == []
    assert decision_client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("system_prompt", ["你是群聊助手。", ""])
async def test_interval_mode_loads_group_context_and_gates_generation(system_prompt: str) -> None:
    """机器人提示词留空时仍可构建决策上下文并按决策结果提交任务。"""
    contexts = {"88888": "第一群最新消息", "99999": "第二群最新消息"}
    decision_client = FakeDecisionClient([decision(False), decision(True, 0.9)])
    context_store = FakeContextStore(contexts)
    job_submitter = FakeJobSubmitter()
    bot = FakeBot()
    service = make_service(
        job_submitter=job_submitter,
        decision_client=decision_client,
        context_store=context_store,
        bot_provider=lambda bot_id: bot,
        group_ids={"99999", "88888"},
        system_prompt=system_prompt,
    )

    result = await service.run_once()

    assert result == AutoSpeakCycleResult(bot_available=True, sent=1, failed=0)
    assert context_store.calls == [("90009", "88888"), ("90009", "99999")]
    assert len(decision_client.calls) == 2
    captured_contexts = [
        decision_payload(call["state"])["untrusted_data"]["recent_group_context"]
        for call in decision_client.calls
    ]
    assert captured_contexts == ["第一群最新消息", "第二群最新消息"]
    expected_session = external_session_id(
        "bot:90009:auto:group:99999",
        "auto-speak-test-secret",
    )
    assert decision_client.calls[1]["session_id"] == expected_session
    assert job_submitter.jobs[0].session_id == expected_session
    assert job_submitter.jobs[0].recent_group_context == "第二群最新消息"
    assert job_submitter.jobs[0].group_id == "99999"
    assert bot.calls == []


@pytest.mark.asyncio
async def test_decision_error_skips_generation_and_does_not_block_later_group() -> None:
    decision_client = FakeDecisionClient(
        [ChatAiUnavailableError("safe unavailable"), decision(True)]
    )
    job_submitter = FakeJobSubmitter()
    bot = FakeBot()
    errors: list[str] = []
    service = make_service(
        job_submitter=job_submitter,
        decision_client=decision_client,
        context_store=FakeContextStore({"88888": "一", "99999": "二"}),
        bot_provider=lambda bot_id: bot,
        group_ids={"88888", "99999"},
        on_error=errors.append,
    )

    result = await service.run_once()

    assert result == AutoSpeakCycleResult(bot_available=True, sent=1, failed=1)
    assert [job.group_id for job in job_submitter.jobs] == ["99999"]
    assert errors == ["ChatAiUnavailableError"]
    assert bot.calls == []


@pytest.mark.asyncio
async def test_message_mode_accepts_only_configured_bot_and_groups() -> None:
    service = make_service(
        job_submitter=FakeJobSubmitter(),
        decision_client=FakeDecisionClient([]),
        context_store=FakeContextStore({"88888": None}),
        bot_provider=lambda bot_id: None,
        trigger_mode="message",
    )

    assert service.runs_on_interval is False
    assert service.accepts_message("90009", "88888") is True
    assert service.accepts_message("other-bot", "88888") is False
    assert service.accepts_message("90009", "99999") is False


@pytest.mark.asyncio
async def test_message_decision_error_is_silent_and_never_sends() -> None:
    errors: list[str] = []
    job_submitter = FakeJobSubmitter()
    bot = FakeBot()
    service = make_service(
        job_submitter=job_submitter,
        decision_client=FakeDecisionClient([ChatAiUnavailableError("safe unavailable")]),
        context_store=FakeContextStore({"88888": "当前消息"}),
        bot_provider=lambda bot_id: bot,
        trigger_mode="message",
        on_error=errors.append,
    )

    sent = await service.handle_group_message(bot, "88888", "当前消息")

    assert sent is False
    assert job_submitter.jobs == []
    assert bot.calls == []
    assert errors == ["ChatAiUnavailableError"]


@pytest.mark.asyncio
async def test_speak_preserves_context_and_auto_reply_limit_in_queued_job() -> None:
    """自动发言的群上下文与专用上限随普通会话任务持久化。"""

    context = "小明(QQ 10001)：今晚开黑吗？"
    max_reply_chars = 20
    job_submitter = FakeJobSubmitter()
    bot = FakeBot()
    service = make_service(
        job_submitter=job_submitter,
        decision_client=FakeDecisionClient([decision(True)]),
        context_store=FakeContextStore({"88888": context}),
        bot_provider=lambda bot_id: bot,
        trigger_mode="message",
        max_reply_chars=max_reply_chars,
    )

    sent = await service.handle_group_message(bot, "88888", context)

    assert sent is True
    assert job_submitter.jobs[0].recent_group_context == context
    assert job_submitter.jobs[0].auto_speak_max_reply_chars == max_reply_chars
    assert bot.calls == []


@pytest.mark.asyncio
async def test_auto_speak_keeps_anonymous_group_session_for_normal_chat_history() -> None:
    """同一自动发言群持续使用匿名会话，让普通聊天队列复用历史。"""

    job_submitter = FakeJobSubmitter()
    bot = FakeBot()
    secret = "a-different-auto-speak-secret"
    service = make_service(
        job_submitter=job_submitter,
        decision_client=FakeDecisionClient([decision(True), decision(True)]),
        context_store=FakeContextStore({"88888": "群聊上下文"}),
        bot_provider=lambda bot_id: bot,
        session_secret=secret,
    )

    await service.run_once()
    await service.run_once()

    expected_session = external_session_id("bot:90009:auto:group:88888", secret)
    assert [job.session_id for job in job_submitter.jobs] == [
        expected_session,
        expected_session,
    ]
    assert expected_session.startswith("qq_")
    assert "90009" not in expected_session
    assert "88888" not in expected_session


@pytest.mark.asyncio
async def test_laya_decision_reads_current_auto_session_history() -> None:
    """Laya 判断读取同一群的自动发言 session 记忆和近期轮次。"""

    provider = FakeConversationContextProvider(
        ConversationContext(
            memory="群成员偏好简短回答",
            messages=(
                ChatMessage(role="user", content="自动发言触发"),
                ChatMessage(role="assistant", content="上次主动说的话"),
            ),
        )
    )
    decision_client = FakeDecisionClient([decision(False)])
    service = make_service(
        job_submitter=FakeJobSubmitter(),
        decision_client=decision_client,
        context_store=FakeContextStore({"88888": "当前群聊"}),
        bot_provider=lambda bot_id: FakeBot(),
        conversation_context_provider=provider,
    )

    await service.run_once()

    expected_session = external_session_id(
        "bot:90009:auto:group:88888",
        "auto-speak-test-secret",
    )
    assert provider.calls == [expected_session]
    payload = decision_payload(decision_client.calls[0]["state"])
    untrusted = payload["untrusted_data"]
    assert isinstance(untrusted, dict)
    assert "群成员偏好简短回答" in untrusted["session_context"]
    assert "上次主动说的话" in untrusted["session_context"]


@pytest.mark.asyncio
async def test_one_group_enqueue_failure_does_not_block_later_group() -> None:
    """单群入队失败只计入本群，后续群仍继续执行决策。"""

    bot = FakeBot()
    job_submitter = FakeJobSubmitter(failing_groups={"88888"})
    errors: list[str] = []
    service = make_service(
        job_submitter=job_submitter,
        decision_client=FakeDecisionClient([decision(True), decision(True)]),
        context_store=FakeContextStore({"88888": "一", "99999": "二"}),
        bot_provider=lambda bot_id: bot,
        group_ids={"99999", "88888"},
        on_error=errors.append,
    )

    result = await service.run_once()

    assert result == AutoSpeakCycleResult(bot_available=True, sent=1, failed=1)
    assert [job.group_id for job in job_submitter.jobs] == ["99999"]
    assert errors == ["RuntimeError"]


@pytest.mark.asyncio
async def test_run_is_interval_only_and_waits_between_complete_cycles() -> None:
    sleeps: list[float] = []
    interval_sleep_started = asyncio.Event()
    keep_sleeping = asyncio.Event()

    async def controlled_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) == 2:
            interval_sleep_started.set()
            await keep_sleeping.wait()

    interval_service = make_service(
        job_submitter=FakeJobSubmitter(),
        decision_client=FakeDecisionClient([decision(True)]),
        context_store=FakeContextStore({"88888": "上下文"}),
        bot_provider=lambda bot_id: FakeBot(),
        initial_delay_seconds=12,
        interval_seconds=75,
        sleep=controlled_sleep,
    )
    task = asyncio.create_task(interval_service.run())
    await interval_sleep_started.wait()

    assert interval_service.runs_on_interval is True
    assert sleeps == [12, 75]

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    message_service = make_service(
        job_submitter=FakeJobSubmitter(),
        decision_client=FakeDecisionClient([]),
        context_store=FakeContextStore({"88888": None}),
        bot_provider=lambda bot_id: FakeBot(),
        trigger_mode="message",
        sleep=controlled_sleep,
    )
    await message_service.run()
    assert sleeps == [12, 75]


@pytest.mark.asyncio
async def test_cancelled_decision_propagates_without_processing_more_groups() -> None:
    class CancellingDecisionClient(FakeDecisionClient):
        async def decide_auto_speak(
            self,
            *,
            state: str,
            session_id: str,
            request_id: str,
        ) -> AutoSpeakDecisionResponse:
            self.calls.append(
                {"state": state, "session_id": session_id, "request_id": request_id}
            )
            raise asyncio.CancelledError

    decision_client = CancellingDecisionClient([])
    service = make_service(
        job_submitter=FakeJobSubmitter(),
        decision_client=decision_client,
        context_store=FakeContextStore({"88888": "一", "99999": "二"}),
        bot_provider=lambda bot_id: FakeBot(),
        group_ids={"88888", "99999"},
    )

    with pytest.raises(asyncio.CancelledError):
        await service.run_once()

    assert len(decision_client.calls) == 1


@pytest.mark.asyncio
async def test_speak_enqueues_normal_group_chat_with_all_selected_tools() -> None:
    """Laya 只做门控，主动发言复用普通群聊队列及其多工具能力。"""

    jobs: list[ConversationJob] = []
    available_tools: tuple[ChatToolName, ...] = (
        "memory",
        "job",
        "mention_member",
        "local_workflow_list",
        "local_workflow_generate_image",
        "tts_generate",
    )

    async def enqueue(job: ConversationJob) -> str:
        """记录自动发言提交的普通会话任务。"""

        jobs.append(job)
        return job.job_id

    bot = FakeBot()
    service = AutoSpeakService(
        job_submitter=enqueue,
        available_tools_provider=lambda: available_tools,
        conversation_context_provider=FakeConversationContextProvider(),
        decision_client=FakeDecisionClient([decision(True)]),
        context_builder=AutoSpeakContextBuilder(),
        recent_context_store=FakeContextStore({"88888": "群聊上下文"}),
        bot_provider=lambda bot_id: bot,
        bot_id="90009",
        group_ids={"88888"},
        trigger_mode="interval",
        prompt="生成一句自然的群聊开场白",
        system_prompt="你是群聊助手。",
        laya_prompt="结合上下文判断是否应该主动发言。",
        session_secret="auto-speak-test-secret",
        initial_delay_seconds=10,
        interval_seconds=60,
        max_reply_chars=321,
    )

    result = await service.run_once()

    assert result == AutoSpeakCycleResult(bot_available=True, sent=1, failed=0)
    assert bot.calls == []
    assert len(jobs) == 1
    job = jobs[0]
    assert job.scene == "group"
    assert job.group_id == "88888"
    assert job.bot_id == "90009"
    assert job.user_id == "90009"
    assert job.prompt == "请结合当前自动发言会话历史和群聊上下文，生成本次主动发言。"
    assert job.system_prompt_append == "生成一句自然的群聊开场白"
    assert job.recent_group_context == "群聊上下文"
    assert job.chat_tools == available_tools
    assert job.auto_speak_max_reply_chars == 321
