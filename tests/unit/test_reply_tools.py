"""工具增强对话统一发送最终正文的离线回归。"""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest
from test_chat_tools import MentionBot, mention_composer, mention_gateway

from agent_bot.config import RuntimeConfig
from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.integrations.tool_operations import RedisToolOperationStore
from agent_bot.services.chat_tools import (
    ChatToolContext,
    ChatToolError,
    build_chat_tool_registry,
)
from agent_bot.services.conversation_queue import ConversationJob, ConversationResult
from agent_bot.services.naraka import NarakaPlayerSearchResult
from agent_bot.services.replies import LlmReplyComposer
from agent_bot.services.workflow_execution import GeneratedWorkflowImage, WorkflowImageCollector


def reply_context(**options) -> ChatToolContext:
    """只授权当前群和当前请求，平台发送由测试观测。"""
    return ChatToolContext(
        session_id="session", user_id="10001", group_id=12345, bot_user_id="90001",
        bot=MentionBot(AsyncMock(return_value={"message_id": 12})),
        group_members=[{"user_id": 10086, "nickname": "小明", "card": ""}],
        allowed_tools=frozenset({"send_reply", "mention_member", "naraka_search"}),
        **options,
    )


@pytest.mark.asyncio
async def test_send_reply_is_no_longer_callable() -> None:
    """即使旧调用方传入权限，也不能让工具替代最终正文发送消息。"""
    context = reply_context()
    with pytest.raises(ChatToolError):
        await build_chat_tool_registry().call("send_reply", {"text": "不应发送"}, context)
    context.bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_mention_intent_survives_new_context_and_rejects_a_second_intent() -> None:
    """队列重试重建上下文后，不能通过改写提醒意图登记另一条提醒。"""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    try:
        store = RedisToolOperationStore(redis, key_prefix="test:reply", ttl_seconds=60)
        registry = build_chat_tool_registry()
        arguments = {"target": "小明", "text": "轮到你了"}
        first = await registry.call(
            "mention_member", arguments, reply_context(job_id="job", operation_store=store),
        )
        retry = reply_context(job_id="job", operation_store=store)
        assert await registry.call("mention_member", arguments, retry) == first
        with pytest.raises(ChatToolError):
            await registry.call("mention_member", {"target": "小明", "text": "再说一句"}, retry)
        assert retry.reply_collector.reply.target_user_id == 10086
        retry.bot.call_api.assert_not_awaited()
    finally:
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("use_query", [False, True])
@pytest.mark.parametrize("choice,final_fails,with_image", [
    (None, False, False), ("mention_member", False, False),
    ("mention_member", True, False), ("mention_member", False, True), (None, False, True),
])
async def test_agent_always_delivers_final_text_once(
    choice: str | None, final_fails: bool, with_image: bool, use_query: bool,
) -> None:
    """文字只发送一次；图片先发送，再以独立完成通知提醒原创建人。"""
    context = reply_context(
        naraka_service=SimpleNamespace(search_players=AsyncMock(
            return_value=NarakaPlayerSearchResult(query="小明"),
        )),
    )
    registry = build_chat_tool_registry()
    images = WorkflowImageCollector() if with_image else None

    async def generate(*args, **kwargs):
        """模拟模型先查询及选择提醒目标，再生成实际对外正文。"""
        if use_query:
            await registry.call("naraka_search", {"nickname": "小明"}, context)
        if images is not None:
            await images.add(GeneratedWorkflowImage(
                media_type="image/png", data=b"\x89PNG\r\n\x1a\nimage",
            ))
        if choice is not None:
            arguments = {"text": "最终回答"}
            if choice == "mention_member":
                arguments["target"] = "小明"
            await registry.call(choice, arguments, context)
        if final_fails:
            raise ConnectionError("agent final response lost")
        return "哪里不对？说错什么了我改，但你得说清楚呀。"

    @asynccontextmanager
    async def runtime():
        """离线运行时不持有外部资源。"""
        yield

    @asynccontextmanager
    async def session(job):
        """工具执行与队列共享同一次回复选择。"""
        yield SimpleNamespace(
            tools=job.chat_tools, token="a" * 40,
            collector=images, speech_collector=None, reply_collector=context.reply_collector,
        )

    chat = SimpleNamespace(
        generate_queued=AsyncMock(side_effect=generate), commit_queued=AsyncMock(),
    )
    completion_chat = SimpleNamespace(generate_reply_queued=AsyncMock(return_value="绘图完成啦。"))
    composer = LlmReplyComposer(lambda: completion_chat, timeout_seconds_provider=lambda: 300)
    config = RuntimeConfig(
        onebot_v11_access_token="test-token-that-is-longer-than-32-characters",
        chat_session_secret="test-secret-that-is-longer-than-32-characters",
    )
    processor = ConversationQueueProcessor(
        config_provider=lambda: config, chat_service_provider=lambda: chat,
        runtime_lease=runtime, tool_session_provider=session, bot_provider=lambda _: context.bot,
        reply_composer=composer, reply_gateway=mention_gateway(),
        operation_store_provider=lambda: context.operation_store,
        image_submitter=AsyncMock(), workflow_service_provider=lambda: None,
    )
    job = ConversationJob(
        jobId="job", requestId="request", createdAt=1, sessionId="session", botId="90001",
        scene="group", groupId="12345", userId="10001", prompt="看看战绩",
        chatTools=("mention_member", "naraka_search"),
    )
    if final_fails:
        with pytest.raises(ConnectionError):
            await processor.generate(job)
        context.bot.call_api.assert_not_awaited()
        return
    result = await processor.generate(job)
    context.bot.call_api.assert_not_awaited()
    restored = ConversationResult.model_validate_json(result.model_dump_json(by_alias=True))
    async with fakeredis.aioredis.FakeRedis(decode_responses=True) as redis:
        # 每个参数独占操作记录，完成通知持久化后由真实 Gateway 投递。
        context.operation_store = RedisToolOperationStore(
            redis, key_prefix="test:reply-completion", ttl_seconds=60,
        )
        count = await processor.delivery_count(job, restored)
        assert count == (2 if with_image else 1)
        await processor.deliver_part(job, restored, 0)
        context.bot.call_api.assert_awaited_once()
        message = context.bot.call_api.await_args.kwargs["message"]
        expected = "" if with_image else "哪里不对？说错什么了我改，但你得说清楚呀。"
        assert message.extract_plain_text().strip() == expected
        assert bool(message["at"]) == (choice == "mention_member" and not with_image)
        assert bool(message["image"]) == with_image
        completion_chat.generate_reply_queued.assert_not_awaited()
        if with_image:
            await processor.deliver_part(job, restored, 1)
            assert context.bot.call_api.await_count == 2
            completion = context.bot.call_api.await_args.kwargs["message"]
            assert completion.extract_plain_text().strip() == "绘图完成啦。"
            assert [segment.data["qq"] for segment in completion["at"]] == [job.user_id]
            assert not completion["image"]
            completion_chat.generate_reply_queued.assert_awaited_once()
            assert completion_chat.generate_reply_queued.await_args.args[0] == "image:job"
            assert completion_chat.generate_reply_queued.await_args.kwargs["use_agent"] is True
        await processor.commit(job, restored)
    chat.generate_queued.assert_awaited_once()
    chat.commit_queued.assert_awaited_once()
    assert chat.commit_queued.await_args.args[2] == ("[已生成图片]" if with_image else expected)


@pytest.mark.asyncio
async def test_concurrent_mention_calls_cannot_register_two_intents() -> None:
    """同一模型轮次并发登记两条不同提醒意图时，只能保留一条。"""
    context = reply_context()
    registry = build_chat_tool_registry()
    results = await asyncio.gather(
        registry.call("mention_member", {"target": "小明", "text": "普通回复"}, context),
        registry.call("mention_member", {"target": "小明", "text": "提醒回复"}, context),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ChatToolError) for result in results) == 1
    assert context.reply_collector.reply.text in {"普通回复", "提醒回复"}
    context.bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_speak_queue_uses_its_stricter_reply_limit() -> None:
    """普通会话处理器按自动发言上限裁剪并分段最终正文。"""

    @asynccontextmanager
    async def runtime():
        """提供无外部资源的队列运行时。"""

        yield

    @asynccontextmanager
    async def session(_job):
        """自动发言未选本地工具时不创建 MCP 会话。"""

        yield ((), None)

    chat = SimpleNamespace(
        generate_queued=AsyncMock(return_value="abcdefghij"),
        commit_queued=AsyncMock(),
    )
    config = RuntimeConfig(
        onebot_v11_access_token="test-token-that-is-longer-than-32-characters",
        chat_session_secret="test-secret-that-is-longer-than-32-characters",
        chat_chunk_chars=3,
        chat_max_reply_chars=7,
    )
    processor = ConversationQueueProcessor(
        config_provider=lambda: config,
        chat_service_provider=lambda: chat,
        runtime_lease=runtime,
        tool_session_provider=session,
        bot_provider=lambda _: MentionBot(AsyncMock()),
        reply_composer=mention_composer(),
        reply_gateway=mention_gateway(),
        operation_store_provider=lambda: None,
        image_submitter=AsyncMock(),
        workflow_service_provider=lambda: None,
    )
    job = ConversationJob(
        jobId="auto-job",
        requestId="auto-request",
        createdAt=1,
        sessionId="auto-session",
        botId="90001",
        scene="group",
        groupId="12345",
        userId="90001",
        prompt="主动问候",
        autoSpeakMaxReplyChars=5,
        systemPromptAppend="自然接续群聊。",
    )

    result = await processor.generate(job)

    assert result.reply is not None
    assert result.reply.text == "abcde"
    assert result.delivery_parts == ("abc", "de")
    assert chat.generate_queued.await_args.kwargs["system_prompt_append"] == "自然接续群聊。"


@pytest.mark.asyncio
async def test_auto_speak_queue_failure_stays_silent() -> None:
    """自动发言队列失败不生成错误解释，避免机器人主动播报内部故障。"""

    composer = SimpleNamespace(compose=AsyncMock())
    processor = ConversationQueueProcessor(
        config_provider=lambda: RuntimeConfig(
            onebot_v11_access_token="test-token-that-is-longer-than-32-characters",
            chat_session_secret="test-secret-that-is-longer-than-32-characters",
        ),
        chat_service_provider=lambda: SimpleNamespace(),
        runtime_lease=AsyncMock(),
        tool_session_provider=AsyncMock(),
        bot_provider=AsyncMock(),
        reply_composer=composer,
        reply_gateway=mention_gateway(),
        operation_store_provider=lambda: None,
        image_submitter=AsyncMock(),
        workflow_service_provider=lambda: None,
    )
    job = ConversationJob(
        jobId="auto-failure",
        requestId="auto-failure",
        createdAt=1,
        sessionId="auto-session",
        botId="90001",
        scene="group",
        groupId="12345",
        userId="90001",
        prompt="主动问候",
        autoSpeakMaxReplyChars=500,
    )

    await processor.deliver_failure(job, RuntimeError("private-detail"))

    composer.compose.assert_not_awaited()
