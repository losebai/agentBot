"""Proactive text must cross the shared model-provenance boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

from agent_bot.config import RuntimeConfig
from agent_bot.integrations.onebot.auto_speak import AutoSpeakService
from agent_bot.integrations.onebot.replies import OneBotReplyGateway
from agent_bot.integrations.onebot.tooling import ChatToolSessionManager
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import AutoSpeakDecisionResponse, ChatContext
from agent_bot.services.auto_speak_decision import AutoSpeakContextBuilder
from agent_bot.services.chat import ChatService
from agent_bot.services.chat.errors import ChatBusyError
from agent_bot.services.chat_tools import (
    ChatToolContext,
    ChatToolError,
    build_chat_tool_registry,
)
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.services.replies import (
    CommandResultFacts,
    GeneratedReply,
    LlmReplyComposer,
    ReplyIntent,
)
from agent_bot.services.tool_sessions import ToolSessionStore
from agent_bot.state import ConversationContext, ConversationStore, external_session_id


def reply_gateway() -> OneBotReplyGateway:
    """Create a real gateway with offline runtime configuration."""

    config = RuntimeConfig(
        onebot_v11_access_token="test-token-that-is-longer-than-32-characters",
        chat_session_secret="test-secret-that-is-longer-than-32-characters",
    )
    return OneBotReplyGateway(lambda: config)


class RecordingGateway(OneBotReplyGateway):
    """Inspect provenance without replacing real gateway validation or delivery."""

    def __init__(self) -> None:
        """Initialize runtime limits and capture only accepted generated replies."""

        super().__init__(reply_gateway()._config_provider)
        self.replies: list[GeneratedReply] = []

    async def send_bot(self, bot, **kwargs):
        """Capture the provenance object passed to the real sender."""

        self.replies.append(kwargs["reply"])
        return await super().send_bot(bot, **kwargs)


class SpeakDecisionClient:
    async def decide_auto_speak(
        self,
        *,
        state: str,
        session_id: str,
        request_id: str,
    ) -> AutoSpeakDecisionResponse:
        """Always allow generation without contacting dispatch-ai."""

        return AutoSpeakDecisionResponse(
            should_speak=True,
            confidence=0.9,
            model="multilingual",
        )


class StaticContextStore:
    def context(self, *, bot_id: str, group_id: str) -> str:
        """Return deterministic context for the provenance boundary tests."""

        return "近期群聊"


def auto_speak_decision_options() -> dict[str, object]:
    """Build the common offline Laya decision dependencies."""

    async def conversation_context(_session_id: str) -> ConversationContext:
        """为主动发言测试提供空 session 历史。"""

        return ConversationContext(memory="", messages=())

    return {
        "decision_client": SpeakDecisionClient(),
        "context_builder": AutoSpeakContextBuilder(),
        "recent_context_store": StaticContextStore(),
        "conversation_context_provider": conversation_context,
        "trigger_mode": "interval",
        "system_prompt": "你是群聊助手。",
        "laya_prompt": "结合上下文判断是否主动发言。",
    }


@pytest.mark.asyncio
async def test_auto_speak_crosses_the_normal_queue_boundary_once() -> None:
    """主动发言决策只提交一次普通会话任务，不绕过队列直接投递。"""

    bot = SimpleNamespace(self_id="90001", send_group_msg=AsyncMock())
    jobs: list[ConversationJob] = []

    async def enqueue(job: ConversationJob) -> str:
        """记录进入普通会话队列的任务。"""

        jobs.append(job)
        return job.job_id

    service = AutoSpeakService(
        job_submitter=enqueue, available_tools_provider=lambda: ("memory",),
        bot_provider=lambda _: bot,
        bot_id="90001", group_ids={"12345"}, prompt="主动说话",
        session_secret="test-secret", initial_delay_seconds=0, interval_seconds=60,
        max_reply_chars=500, **auto_speak_decision_options(),
    )

    result = await service.run_once()

    assert result.sent == 1
    assert len(jobs) == 1
    assert jobs[0].chat_tools == ("memory",)
    bot.send_group_msg.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_speak_enqueue_failure_never_sends() -> None:
    """普通会话入队失败保持群侧静默，并只上报异常类型。"""

    bot = SimpleNamespace(self_id="90001", send_group_msg=AsyncMock())
    errors: list[str] = []

    async def enqueue(job: ConversationJob) -> str:
        """模拟普通会话队列拒绝任务。"""

        raise RuntimeError("private-queue-detail")

    service = AutoSpeakService(
        job_submitter=enqueue, available_tools_provider=lambda: (),
        bot_provider=lambda _: bot, bot_id="90001", group_ids={"12345"},
        prompt="主动说话", session_secret="test-secret", initial_delay_seconds=0,
        interval_seconds=60, max_reply_chars=500, on_error=errors.append,
        **auto_speak_decision_options(),
    )

    result = await service.run_once()

    assert result.failed == 1
    bot.send_group_msg.assert_not_awaited()
    assert errors == ["RuntimeError"]


def mention_context(*, answer="模型生成的提醒", target="10086", allowed=True):
    """Bind real composition and gateway components to offline external adapters."""

    chat = SimpleNamespace(generate_reply_queued=AsyncMock(return_value=answer))
    if isinstance(answer, Exception):
        chat.generate_reply_queued.side_effect = answer
    bot = SimpleNamespace(send_group_msg=AsyncMock(return_value={"message_id": 456}))
    gateway = RecordingGateway()
    context = ChatToolContext(
        session_id="group:123", user_id="10001", user_name="发起人",
        chat_context=ChatContext(
            scene="group", groupId="12345", senderId="10001", senderName="发起人"
        ),
        group_id=12345, bot_user_id="90001", bot=bot,
        allowed_tools=frozenset({"mention_member"}) if allowed else frozenset(),
        group_members=[{"user_id": int(target), "nickname": "小明", "card": ""}],
    )
    return context, chat, bot, gateway


@pytest.mark.asyncio
async def test_mention_uses_model_tool_text_without_a_child_request() -> None:
    """模型工具正文直接登记为最终回复，不让子请求再改写或发送。"""
    context, chat, bot, gateway = mention_context()
    registry = build_chat_tool_registry()
    arguments = {"target": "10086", "text": "请看群消息"}
    result = await registry.call("mention_member", arguments, context)
    assert await registry.call("mention_member", arguments, context) == result
    assert context.reply_collector.reply.text == arguments["text"]
    assert context.reply_collector.reply.target_user_id == 10086
    chat.generate_reply_queued.assert_not_awaited()
    bot.send_group_msg.assert_not_awaited()
    assert gateway.replies == []


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["", "   ", "x" * 501])
async def test_mention_invalid_tool_text_is_rejected_without_model_or_send(text) -> None:
    """空或超长工具正文不会另发子请求或发送消息。"""
    context, chat, bot, gateway = mention_context()
    with pytest.raises(ChatToolError) as error:
        await build_chat_tool_registry().call(
            "mention_member", {"target": "10086", "text": text}, context,
        )
    assert error.value.code == "INVALID_ARGUMENT"
    chat.generate_reply_queued.assert_not_awaited()
    assert gateway.replies == []
    bot.send_group_msg.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed,target", [(False, "10086"), (True, "90001")])
async def test_mention_authorization_precedes_composition_and_delivery(allowed, target) -> None:
    """Unauthorized tool or bot-target attempts remain structured tool errors only."""

    context, chat, bot, gateway = mention_context(allowed=allowed, target=target)
    with pytest.raises(ChatToolError) as error:
        await build_chat_tool_registry().call(
            "mention_member", {"target": target, "text": "提醒意图"}, context
        )

    assert error.value.code == "FORBIDDEN"
    chat.generate_reply_queued.assert_not_awaited()
    assert gateway.replies == []
    bot.send_group_msg.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_tool_sessions_bind_shared_reply_collector_and_caller_context(queued) -> None:
    """工具会话与队列共享唯一回复选择，并保留调用者上下文。"""

    context, chat, bot, _gateway = mention_context()
    bot.self_id = "90001"
    config = RuntimeConfig(
        onebot_v11_access_token="test-token-that-is-longer-than-32-characters",
        chat_session_secret="test-secret-that-is-longer-than-32-characters",
        chat_mcp_enabled=True, chat_mention_tool_enabled=True,
    )
    store = ToolSessionStore(ttl_seconds=30, max_sessions=2)
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: store,
        naraka_service_provider=lambda: None,
        group_member_refresher=AsyncMock(return_value=context.group_members),
        chat_context_provider=lambda _: context.chat_context,
    )
    event = GroupMessageEvent(
        time=1, self_id=90001, post_type="message", sub_type="normal",
        user_id=10001, group_id=12345, message_type="group", message_id=1,
        message=Message("提醒"), original_message=Message("提醒"), raw_message="提醒",
        font=0, sender={"user_id": 10001, "nickname": "发起人"},
    )
    job = ConversationJob(
        jobId="job-mention", requestId="request-mention", createdAt=1,
        sessionId=context.session_id, botId="90001", scene="group", groupId="12345",
        userId="10001", userName="发起人", chatContext=context.chat_context,
        prompt="提醒", chatTools=("mention_member",),
    )
    manager.queued_bot = lambda _: bot
    lease = (
        manager.open_queued_session(job) if queued
        else manager.open_chat_session(bot, event, context.session_id)
    )

    async with lease as session:
        bound = await store.resolve(session.token)
        assert bound.reply_collector is session.reply_collector
        assert bound.chat_context is context.chat_context
        assert bound.user_name == "发起人"
        await build_chat_tool_registry().call(
            "mention_member", {"target": "10086", "text": "提醒意图"}, bound
        )

    assert session.reply_collector.reply.text == "提醒意图"
    chat.generate_reply_queued.assert_not_awaited()
    bot.send_group_msg.assert_not_awaited()


def real_chat_service(client: SimpleNamespace, *, capacity: int = 1) -> ChatService:
    """Build the real admission/history pipeline with only the external model stubbed."""

    return ChatService(
        client=client,
        store=ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=3600),
        system_prompt="当前人设", max_input_chars=1000,
        cooldown_seconds=60, max_concurrent_requests=capacity,
        model_router=ContentModelRouter(
            text_model="text", vision_model="vision", image_model="image", audio_model="audio"
        ),
    )


def reminder_intent() -> ReplyIntent:
    """Use the same session as the outer Agent when requesting inner composition."""

    return ReplyIntent(
        requestId="inner-mention", sessionId="group:outer", code="COMMAND_RESULT",
        facts=CommandResultFacts(operation="提醒群成员", content="提醒意图"),
    )


@pytest.mark.asyncio
async def test_nested_composer_does_not_reenter_outer_chat_admission() -> None:
    """One outer slot must permit its own tool's real inner Composer call immediately."""

    async def complete(**options: object) -> str:
        """Model the outer Agent awaiting a nested reply generated by the same service."""

        if options.get("use_agent") is False:
            assert options["use_web_search"] is False
            return "内层模型提醒"
        reply = await composer.compose(reminder_intent())
        assert reply is not None
        return reply.text

    client = SimpleNamespace(complete=AsyncMock(side_effect=complete))
    service = real_chat_service(client)
    composer = LlmReplyComposer(lambda: service, timeout_seconds_provider=lambda: 300)

    answer = await asyncio.wait_for(
        service.generate_queued(
            "group:outer", "触发提醒工具", request_id="outer",
            chat_tools=("mention_member",), tool_session_token="test-request-scoped-token",
        ),
        timeout=0.5,
    )

    assert answer == "内层模型提醒"
    assert client.complete.await_count == 2


@pytest.mark.asyncio
async def test_composer_runs_when_outer_capacity_is_saturated_without_bypassing_chat() -> None:
    """Reply composition has separate capacity while ordinary direct/queued chat stays limited."""

    outer_started: list[str] = []
    saturated = asyncio.Event()
    release = asyncio.Event()

    async def complete(**options: object) -> str:
        """Hold both ordinary model slots until independent composition completes."""

        if options.get("request_id") == "inner-mention":
            return "独立回复"
        outer_started.append(str(options["session_id"]))
        if len(outer_started) == 2:
            saturated.set()
        await release.wait()
        return "普通模型回复"

    service = real_chat_service(
        SimpleNamespace(complete=AsyncMock(side_effect=complete)), capacity=2
    )
    composer = LlmReplyComposer(lambda: service, timeout_seconds_provider=lambda: 300)
    tasks = [asyncio.create_task(service.ask(session, "占用槽位")) for session in ("a", "b")]
    try:
        await asyncio.wait_for(saturated.wait(), timeout=0.5)
        with pytest.raises(ChatBusyError):
            await service.ask("c", "普通请求不能绕过上限")
        tasks.append(asyncio.create_task(
            service.generate_queued("queued", "普通排队请求仍须等候", request_id="queued")
        ))
        reply = await asyncio.wait_for(composer.compose(reminder_intent()), timeout=0.5)
        assert reply is not None and reply.text == "独立回复"
        assert set(outer_started) == {"a", "b"}
    finally:
        release.set()
        await asyncio.gather(*tasks)


@pytest.mark.asyncio
async def test_auto_speak_job_preserves_history_scope_and_reply_limit() -> None:
    """队列任务保留匿名历史范围和自动发言专用回复上限。"""

    jobs: list[ConversationJob] = []
    bot = SimpleNamespace(self_id="90001", send_group_msg=AsyncMock())

    async def enqueue(job: ConversationJob) -> str:
        """记录待由普通会话处理器执行的自动发言。"""

        jobs.append(job)
        return job.job_id

    service = AutoSpeakService(
        job_submitter=enqueue, available_tools_provider=lambda: (),
        bot_provider=lambda _: bot, bot_id="90001", group_ids={"12345"},
        prompt="主动问候", session_secret="test-secret", initial_delay_seconds=0,
        interval_seconds=60, max_reply_chars=20, **auto_speak_decision_options(),
    )

    result = await service.run_once()

    session = external_session_id("bot:90001:auto:group:12345", "test-secret")
    assert result.sent == 1
    assert jobs[0].session_id == session
    assert jobs[0].auto_speak_max_reply_chars == 20
    bot.send_group_msg.assert_not_awaited()
