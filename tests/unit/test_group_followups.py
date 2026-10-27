"""离线贯通真实发送确认、群快照与成员不带称呼的追问。"""

from typing import Any
from unittest.mock import AsyncMock

import pytest
from tests.unit.test_auto_speak import FakeDecisionClient, FakeJobSubmitter, decision, make_service
from tests.unit.test_auto_speak_trigger import group_event
from tests.unit.test_outgoing_context import setup_recorder

from agent_bot.ai.chat_errors import ChatAiUnavailableError
from agent_bot.group_context import RecentGroupContextStore
from agent_bot.integrations.onebot.auto_speak_trigger import GroupAutoSpeakTrigger
from agent_bot.memory_store import ChatMemoryStoreError
from agent_bot.services.chat.prompting import ChatPromptBuilder
from agent_bot.services.replies import GeneratedReply
from agent_bot.state import ConversationContext


def group_pipeline(project: Any = None) -> tuple[Any, ...]:
    """复用真实出站登记器，仅替换平台网络和持久化依赖。"""
    recent = RecentGroupContextStore(message_limit=10, max_chars=1000,
                                    ttl_seconds=60, max_groups=10)

    async def record_sent(scope: Any, delivery_id: str, text: str) -> None:
        """接收确认后的可见文本，去重和快照读写使用生产实现。"""
        recent.record(bot_id=scope.bot_id, group_id=scope.group_id, sender_id=scope.bot_id,
                      sender_name="机器人[桃桃]", content=text, delivery_id=delivery_id)

    recorder, gateway, bot, journal, _, _ = setup_recorder()
    recorder.group_message_recorder = project or record_sent
    bot.self_id = "90009"
    bot.send_group_msg = AsyncMock(return_value={"message_id": 22})
    return recorder, gateway, bot, recent, journal


async def test_confirmed_answer_then_followup_reaches_decision_and_generation() -> None:
    """发送真实确认一次，后续“为什么”同时携带机器人回答进入决策和正文队列。"""
    recorder, gateway, bot, recent, _ = group_pipeline()
    reply = GeneratedReply(text="先做第一步。", requestId="first-answer")
    for _ in range(2):
        async with recorder.scope("s", reply.request_id, key="answer", bot_id="90009",
                                  group_id="88888"):
            await gateway.send_bot_exact_part(bot, scene="group", target_id="88888", reply=reply)
    bot.send_group_msg.assert_awaited_once()
    assert recent.context(bot_id="90009", group_id="88888").count(reply.text) == 1
    client, jobs = FakeDecisionClient([decision(True)]), FakeJobSubmitter()
    service = make_service(
        job_submitter=jobs, decision_client=client, context_store=recent,
        bot_provider=lambda _: bot, trigger_mode="message", decision_backend="llm",
    )

    async def record_received(bot: Any, event: Any) -> None:
        """仅模拟已授权成员入站，快照和后续触发继续使用生产实现。"""
        recent.record(bot_id=bot.self_id, group_id=str(event.group_id),
                      sender_id=str(event.user_id), sender_name="成员",
                      content=event.get_plaintext())

    trigger = GroupAutoSpeakTrigger(service_provider=lambda: service,
                                   recent_context_store_provider=lambda: recent,
                                   record_recent_group_text=record_received)
    event = group_event("为什么？", group_id=88888).model_copy(update={"self_id": 90009})
    await trigger.handle(bot, event)
    assert reply.text in client.calls[0]["state"] and "为什么？" in client.calls[0]["state"]
    assert jobs.jobs[0].prompt == "为什么？"
    assert reply.text in jobs.jobs[0].recent_group_context
    messages = ChatPromptBuilder(system_prompt="你是桃桃", max_input_chars=1000).messages(
        jobs.jobs[0].prompt, conversation_context=ConversationContext("", ()),
        recent_group_context=jobs.jobs[0].recent_group_context,
    )
    background = next(message.content for message in messages if reply.text in message.content)
    assert "机器人已确认发送" in background
    assert "追问" in background
    await trigger.handle(bot, event.model_copy(update={"user_id": 90009}))
    assert len(client.calls) == 1


async def test_failed_and_private_sends_do_not_enter_group_snapshot() -> None:
    """未确认平台发送以及私聊消息不得出现在任何群快照里。"""
    project = AsyncMock()
    recorder, gateway, bot, _, _ = group_pipeline(project)
    bot.send_group_msg.side_effect = ConnectionError("offline")
    async with recorder.scope("s", "failed", key="reply", bot_id="90009", group_id="88888"):
        with pytest.raises(ConnectionError):
            await gateway.send_bot_exact_part(
                bot, scene="group", target_id="88888",
                reply=GeneratedReply(text="未发出", requestId="failed"),
            )
    async with recorder.scope("s", "private", key="reply", bot_id="90009"):
        await gateway.send_bot_exact_part(
            bot, scene="private", target_id="10001",
            reply=GeneratedReply(text="私聊内容", requestId="private"),
        )
    project.assert_not_awaited()


async def test_projection_failure_recovers_without_resending() -> None:
    """群快照写失败后复用出站回执补写，不能把补写变成平台重复发送。"""
    project = AsyncMock(side_effect=[ChatMemoryStoreError("offline"), None])
    recorder, gateway, bot, _, _ = group_pipeline(project)
    reply = GeneratedReply(text="已经发出的正文", requestId="retry-projection")
    for attempt in range(2):
        async with recorder.scope("s", reply.request_id, key="answer", bot_id="90009",
                                  group_id="88888"):
            if attempt == 0:
                with pytest.raises(ChatAiUnavailableError):
                    await gateway.send_bot_exact_part(bot, scene="group", target_id="88888",
                                                      reply=reply)
            else:
                await gateway.send_bot_exact_part(
                    bot, scene="group", target_id="88888", reply=reply,
                )
    assert project.await_count == 2
    bot.send_group_msg.assert_awaited_once()


async def test_interval_does_not_reply_to_own_last_message() -> None:
    """已发送的机器人回答仅作背景，不成为定时发言的新输入。"""
    _, _, bot, recent, _ = group_pipeline()
    recent.record(bot_id="90009", group_id="88888", sender_id="90009",
                  sender_name="机器人[桃桃]", content="已回答。")
    client, jobs = FakeDecisionClient([decision(True)]), FakeJobSubmitter()
    service = make_service(job_submitter=jobs, decision_client=client, context_store=recent,
                          bot_provider=lambda _: bot, decision_backend="llm")
    assert (await service.run_once()).sent == 0
    assert not client.calls and not jobs.jobs


@pytest.mark.parametrize("scene", ["private", "group"])
async def test_projection_uses_actual_destination_instead_of_event_scope(scene: str) -> None:
    """来自群事件的私聊或异群发送，不能把正文写回原事件群。"""
    recorder, gateway, bot, recent, _ = group_pipeline()
    async with recorder.scope("s", "different-target", key="reply", bot_id="90009",
                              group_id="88888"):
        await gateway.send_bot_exact_part(
            bot, scene=scene, target_id="77777",
            reply=GeneratedReply(text="只属于实际发送目标", requestId="different-target"),
        )
    assert recent.context(bot_id="90009", group_id="88888") is None
    context = recent.context(bot_id="90009", group_id="77777")
    if scene == "private":
        assert context is None
    else:
        assert "只属于实际发送目标" in context
