from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence

import pytest

from agent_bot.ai.chat_errors import ChatAiUnavailableError
from agent_bot.group_context import RecentGroupContextStore, RecentGroupMessage, RecentGroupSnapshot
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

    def snapshot(self, *, bot_id: str, group_id: str) -> RecentGroupSnapshot:
        """保留原测试的文本快照，同时给出可独立用于正文的当前消息。"""
        context = self.context(bot_id=bot_id, group_id=group_id)
        latest = RecentGroupMessage("10001", None, context) if context else None
        return RecentGroupSnapshot(context, latest)


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
    group_shared_session: bool = False,
    decision_backend: str = "laya",
    forward_user_name: bool = False,
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
        group_shared_session=group_shared_session,
        decision_backend=decision_backend,
        forward_user_name=forward_user_name,
    )


@pytest.mark.parametrize("trigger_mode", ["interval", "message"])
async def test_llm_decision_does_not_read_history_or_agent_persona(trigger_mode):
    """传入近期及当前群聊文本，同时禁止读取会话记忆、历史和 Agent 人设。"""
    from unittest.mock import AsyncMock

    history = AsyncMock(side_effect=AssertionError("must not read session memory/history"))
    persona = AsyncMock(side_effect=AssertionError("must not inherit Agent persona"))
    jobs = FakeJobSubmitter()
    client = FakeDecisionClient([decision(True), decision(False)])
    bot = FakeBot()
    value = make_service(
        job_submitter=jobs, decision_client=client,
        context_store=FakeContextStore({"88888": "近期消息\n当前收到消息"}),
        bot_provider=lambda _: bot, trigger_mode=trigger_mode,
        conversation_context_provider=history, system_prompt_provider=persona,
        system_prompt="不得传入的机器人人设", prompt="不得传入的正文生成指令",
        decision_backend="llm",
    )
    if trigger_mode == "interval":
        assert (await value.run_once()).sent == 1
        assert (await value.run_once()).sent == 0
    else:
        assert await value.handle_group_message(
            bot, "88888", "近期消息\n当前收到消息", trigger_message="当前收到消息",
        )
        assert not await value.handle_group_message(
            bot, "88888", "近期消息\n当前收到消息", trigger_message="当前收到消息",
        )
    history.assert_not_awaited()
    persona.assert_not_awaited()
    assert len(jobs.jobs) == 1
    for call in client.calls:
        assert call["state"] == "近期消息\n当前收到消息"


@pytest.mark.parametrize(
    "context", [None, "", "过旧消息" + "旧" * 9000 + "最新消息"],
    ids=["missing", "empty", "over-limit"],
)
async def test_llm_decision_context_is_bounded_and_keeps_latest_text(context):
    """空快照不调用模型，过长快照优先保留最新文本并遵守八千字符边界。"""
    client = FakeDecisionClient([decision(False)])
    value = make_service(
        job_submitter=FakeJobSubmitter(), decision_client=client,
        context_store=FakeContextStore({"88888": context}), bot_provider=lambda _: FakeBot(),
        decision_backend="llm",
    )
    await value.run_once()
    if not context:
        assert client.calls == []
        return
    state = client.calls[0]["state"]
    assert 0 < len(state) <= 8000
    assert state.endswith("最新消息")
    assert "过旧消息" not in state


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

    sent = await service.handle_group_message(bot, "88888", "当前消息", trigger_message="当前消息")

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

    sent = await service.handle_group_message(bot, "88888", context, trigger_message=context)

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
@pytest.mark.parametrize("trigger_mode", ["interval", "message"])
@pytest.mark.parametrize("group_shared_session", [True, False])
async def test_auto_speak_material_owner_matches_shared_group_only(
    tmp_path, trigger_mode, group_shared_session,
) -> None:
    """共享群的主动发言可被普通后续消息读取，非共享群仍保持用户与私聊隔离。"""
    import sqlite3
    from pathlib import Path
    from types import SimpleNamespace

    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

    from agent_bot.database import database_connector
    from agent_bot.integrations.onebot.context import EventContextService
    from agent_bot.materials.store import MaterialStore
    from agent_bot.media.contracts import MediaOwner
    from agent_bot.media.errors import MediaError
    from agent_bot.media.files import MediaFiles
    from agent_bot.settings import DatabaseParams

    bot = FakeBot()
    secret = "auto-speak-shared-session-secret"
    submitter, context_provider = FakeJobSubmitter(), FakeConversationContextProvider()
    client = FakeDecisionClient([decision(True)])
    service = make_service(
        job_submitter=submitter, decision_client=client,
        context_store=FakeContextStore({"88888": "群聊上下文"}), bot_provider=lambda _: bot,
        session_secret=secret, conversation_context_provider=context_provider,
        trigger_mode=trigger_mode, group_shared_session=group_shared_session,
    )
    if trigger_mode == "interval":
        assert (await service.run_once()).sent == 1
    else:
        assert await service.handle_group_message(
            bot, "88888", "群聊上下文", trigger_message="群聊上下文",
        )
    job = submitter.jobs[0]
    expected_scope = (
        "bot:90009:group:88888" if group_shared_session else "bot:90009:auto:group:88888"
    )
    expected_session = external_session_id(expected_scope, secret)
    assert job.session_id == expected_session
    assert context_provider.calls == [expected_session]
    assert client.calls[0]["session_id"] == expected_session

    event_context = EventContextService(
        config_provider=lambda: SimpleNamespace(chat_group_shared_session=group_shared_session),
        access_policy_provider=lambda: None, session_secret_provider=lambda: secret,
        group_member_store_provider=lambda: None, recent_context_store_provider=lambda: None,
    )
    event = GroupMessageEvent(
        time=1, self_id=90009, post_type="message", sub_type="normal", user_id=10001,
        message_type="group", message_id=1, message=Message("这张图"),
        original_message=Message("这张图"), raw_message="这张图", font=0,
        sender={"user_id": 10001, "nickname": "用户", "role": "member"}, group_id=88888,
    )
    followup_owner = MediaOwner(entry="qq", session_id=event_context.session_id(bot, event))
    database_path = tmp_path / "scope.sqlite3"
    schema = await asyncio.to_thread(
        Path("sql/agent_bot_init.sqlite.sql").read_text, encoding="utf-8",
    )
    with sqlite3.connect(database_path) as database:
        database.executescript(schema)
    params = DatabaseParams(backend="sqlite", sqlite_path=str(database_path))
    store = MaterialStore(params, connect=database_connector(params),
                          files=MediaFiles(tmp_path / "materials"))
    await store.start()
    try:
        asset = await store.put(
            owner=MediaOwner(entry="qq", session_id=job.session_id), name="主动发言.txt",
            mime_type="text/plain", data=b"proactive attachment",
        )
        if group_shared_session:
            assert followup_owner.session_id == job.session_id
            assert (await store.describe(asset.id, owner=followup_owner)).id == asset.id
        else:
            assert followup_owner.session_id != job.session_id
            with pytest.raises(MediaError, match="MATERIAL_NOT_FOUND"):
                await store.describe(asset.id, owner=followup_owner)
        private_owner = MediaOwner(entry="qq", session_id=external_session_id(
            "bot:90009:private:10001", secret,
        ))
        with pytest.raises(MediaError, match="MATERIAL_NOT_FOUND"):
            await store.describe(asset.id, owner=private_owner)
    finally:
        await store.aclose()


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
    assert job.prompt == "群聊上下文"
    assert "生成一句自然的群聊开场白" in job.system_prompt_append
    assert job.recent_group_context == "群聊上下文"
    assert job.chat_tools == available_tools
    assert job.auto_speak_max_reply_chars == 321


@pytest.mark.parametrize("trigger_mode", ["interval", "message"])
@pytest.mark.parametrize("backend", ["laya", "llm"])
@pytest.mark.parametrize("generation_prompt", ["", "规则" * 500], ids=["empty", "max-length"])
async def test_auto_speak_uses_real_input_and_shared_multi_reply_rules(
    monkeypatch, chat_plugin, trigger_mode, backend, generation_prompt,
) -> None:
    """两种触发都把对应的真实消息交给正文模型，并经过普通队列的分条规则。"""
    from unittest.mock import AsyncMock

    store = RecentGroupContextStore(
        message_limit=10, max_chars=4000, ttl_seconds=60, max_groups=10,
    )
    for content in ("旧消息", "最新消息第一行\n第二行"):
        store.record(
            bot_id="90009", group_id="88888", sender_id="10001", sender_name="成员",
            content=content,
        )
    jobs = FakeJobSubmitter()
    bot = FakeBot()
    value = make_service(
        job_submitter=jobs, decision_client=FakeDecisionClient([decision(True)]),
        context_store=store, bot_provider=lambda _: bot, trigger_mode=trigger_mode,
        prompt=generation_prompt, decision_backend=backend, max_reply_chars=20,
    )
    if trigger_mode == "interval":
        assert (await value.run_once()).sent == 1
        expected = "最新消息第一行\n第二行"
    else:
        expected = "触发消息原文\n完整第二行"
        assert await value.handle_group_message(
            bot, "88888", "成员：触发消息原文", trigger_message=expected,
        )
    job = jobs.jobs[0]
    assert job.prompt == expected
    if generation_prompt:
        assert job.system_prompt_append.startswith(generation_prompt)
    assert job.auto_speak_max_reply_chars == 20
    assert "请结合当前自动发言会话历史和群聊上下文" not in job.prompt
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_multi_reply_enabled", True)
    agent = AsyncMock(return_value='["第一条", "第二条"]')
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", agent)
    result = await chat_plugin._ConversationProcessor().generate(job)
    assert agent.await_args.args[1] == expected
    assert "JSON" in agent.await_args.kwargs["system_prompt_append"]
    assert result.delivery_parts == ("第一条", "第二条")


async def test_interval_without_latest_message_skips_decision_and_generation() -> None:
    """没有有效群消息时不制造引导句，也不调用模型空聊。"""
    jobs, client = FakeJobSubmitter(), FakeDecisionClient([])
    value = make_service(
        job_submitter=jobs, decision_client=client, context_store=FakeContextStore({"88888": None}),
        bot_provider=lambda _: FakeBot(), decision_backend="llm",
    )
    assert (await value.run_once()).sent == 0
    assert jobs.jobs == [] and client.calls == []


async def test_tool_routing_uses_trigger_message_instead_of_generation_style() -> None:
    """战绩等本地能力按真实群消息选择，风格提示词不能替代本轮话题。"""
    jobs = FakeJobSubmitter()
    bot = FakeBot()
    value = make_service(
        job_submitter=jobs, decision_client=FakeDecisionClient([decision(True)]),
        context_store=FakeContextStore({"88888": "最近消息"}), bot_provider=lambda _: bot,
        trigger_mode="message", prompt="自然参与群聊", available_tools=("naraka_query",),
    )
    assert await value.handle_group_message(
        bot, "88888", "最近消息", trigger_message="帮忙看看永劫无间战绩",
    )
    assert jobs.jobs[0].chat_tools == ("naraka_query",)
