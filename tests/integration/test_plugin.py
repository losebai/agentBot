from __future__ import annotations

import asyncio
import base64
import threading
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import nonebot
import pytest
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    Message,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import (
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    LifecycleMetaEvent,
    Reply,
    Sender,
)
from nonebot.adapters.onebot.v11.exception import NetworkError
from nonebot.exception import FinishedException
from nonebug import App

from agent_bot.ai.chat_errors import ChatAiUnavailableError
from agent_bot.group_context import RecentGroupContextStore
from agent_bot.integrations.onebot.queue_processor import generated_reply_from_model
from agent_bot.memory_store import ChatMemoryStoreError
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import ChatMediaItem, ChatMediaResponse
from agent_bot.multimodal import (
    ImageRecognitionDisabledError,
    InvalidImageAttachmentError,
    InvalidVideoAttachmentError,
    TooManyImagesError,
    TooManyVideosError,
)
from agent_bot.quoted_summary import (
    ForwardMessageUnavailableError,
    QuotedMessageEmptyError,
    QuotedPromptLimitError,
)
from agent_bot.services.chat import ChatBusyError, ChatRateLimitedError, ChatService
from agent_bot.services.chat.request_control import ChatRequestCoordinator
from agent_bot.services.conversation_queue import (
    ConversationJobExpiredError,
    ConversationQueueFullError,
)
from agent_bot.services.naraka import (
    NarakaAuthenticationError,
    NarakaDataUpdatingError,
    NarakaMatchDetail,
    NarakaPlayerSearchResult,
    NarakaResult,
    NarakaUnavailableError,
)
from agent_bot.services.replies import GeneratedReply
from agent_bot.services.workflow_execution import GeneratedWorkflowImage
from agent_bot.state import AccessPolicy, ConversationStore


@pytest.fixture(autouse=True)
def reply_chat(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> AsyncMock:
    """隔离回复模型并让所有校验分支使用可辨认的固定模型输出。"""

    generate = AsyncMock(return_value="模型生成的校验回复。")
    if "chat_plugin" in request.fixturenames:
        chat_plugin = request.getfixturevalue("chat_plugin")
        monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", generate)
    return generate


@pytest.fixture(autouse=True)
async def plugin_materials(request, monkeypatch, tmp_path):
    """插件消息执行真实素材入库，只替换 QQ HTTP 下载为确定的不同图片。"""
    if "chat_plugin" not in request.fixturenames:
        yield None
        return
    import hashlib
    from io import BytesIO
    from types import SimpleNamespace

    from PIL import Image

    from agent_bot.database_pool import create_database_pool
    from agent_bot.materials.receiver import MaterialReceiver
    from agent_bot.materials.store import MaterialStore
    from agent_bot.media.files import MediaFiles
    from agent_bot.media.inputs import ImagePreparer
    from agent_bot.settings import DatabaseParams

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "materials.sqlite3"))
    # 使用仓库当前结构，避免已安装 wheel 的 SQL 副本遮蔽本轮迁移。
    import sqlite3
    from pathlib import Path

    with sqlite3.connect(params.sqlite_path) as connection:
        schema = await asyncio.to_thread(Path("sql/agent_bot_init.sqlite.sql").read_text, "utf-8")
        connection.executescript(schema)
    pool = create_database_pool(params)
    store = MaterialStore(params, connect=pool.connect, files=MediaFiles(tmp_path / "materials"))
    await store.start()

    async def download(url):
        """相同 URL 得到相同内容，不同 URL 得到不同内容以验证 ID 关联。"""
        output = BytesIO()
        color = tuple(hashlib.sha256(url.encode()).digest()[:3])
        Image.new("RGB", (2, 2), color).save(output, "PNG")
        return ImagePreparer().prepare(output.getvalue(), "image/png")

    plugin = request.getfixturevalue("chat_plugin")
    monkeypatch.setattr(plugin, "_material_receiver", MaterialReceiver(
        store, image_downloader=SimpleNamespace(fetch=download),
        file_downloader=SimpleNamespace(fetch=AsyncMock(return_value=(b"document", "text/plain"))),
    ))
    try:
        yield store
    finally:
        await store.aclose()
        pool.close()


@pytest.mark.integration
async def test_untriggered_image_is_not_saved(app, chat_plugin, plugin_materials):
    """图片只有触发对话才接收，普通消息监听不能采集全部聊天图片。"""
    from agent_bot.materials.contracts import MaterialSearchArguments
    from agent_bot.media.contracts import MediaOwner

    event = private_event(MessageSegment.image("https://gchat.qpic.cn/receive.png"))
    receiver = chat_plugin._material_matchers[0]
    async with app.test_matcher(receiver) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="90001")
        ctx.receive_event(bot, event)
    owner = MediaOwner(entry="qq", session_id=chat_plugin._event_context.session_id(bot, event))
    records = (await plugin_materials.search(owner, MaterialSearchArguments())).records
    assert records == ()
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()


async def test_natural_chat_cooldown_does_not_receive_image(
    chat_plugin, plugin_materials, monkeypatch,
):
    """触发对话但被冷却拒绝的图片不能提前进入素材库。"""
    from nonebot.exception import FinishedException

    from agent_bot.materials.contracts import MaterialSearchArguments
    from agent_bot.media.contracts import MediaOwner

    monkeypatch.setattr(chat_plugin.message_counter, "try_consume", lambda _: False)
    bot = Mock(self_id="90001")
    event = private_event(MessageSegment.image("https://qpic.cn/cooldown.png"))
    matcher = Mock(finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            bot, event, matcher=matcher, mention_handler=None,
        )
    owner = MediaOwner(entry="qq", session_id=chat_plugin._event_context.session_id(bot, event))
    assert (await plugin_materials.search(owner, MaterialSearchArguments())).records == ()


@pytest.mark.integration
@pytest.mark.parametrize("group", [False, True])
async def test_file_notice_persists_in_real_event_session(
    app, chat_plugin, plugin_materials, group,
):
    """群上传和私聊离线文件通知均可保存，素材归属与对应聊天会话一致。"""
    from nonebot.adapters.onebot.v11 import GroupUploadNoticeEvent, NoticeEvent

    from agent_bot.materials.contracts import MaterialSearchArguments
    from agent_bot.media.contracts import MediaOwner

    file = {"id": "file-1", "name": "plan.txt", "size": 8}
    if group:
        file["busid"] = 102
        event = GroupUploadNoticeEvent.model_validate({
            "self_id": 90001, "time": 1, "post_type": "notice", "notice_type": "group_upload",
            "group_id": 20001, "user_id": 10001, "file": file,
        })
        message_event = group_event("", user_id=10001)
    else:
        file["url"] = "https://files.qq.com/offline"
        event = NoticeEvent.model_validate({
            "self_id": 90001, "time": 1, "post_type": "notice", "notice_type": "offline_file",
            "user_id": 10001, "file": file,
        })
        message_event = private_event("")
    receiver = chat_plugin._material_matchers[1]
    async with app.test_matcher(receiver) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="90001")
        ctx.receive_event(bot, event)
        if group:
            ctx.should_call_api("get_group_file_url", {
                "group_id": 20001, "file_id": "file-1", "busid": 102,
            }, result={"url": "https://files.qq.com/group"})
    owner = MediaOwner(entry="qq", session_id=chat_plugin._event_context.session_id(
        bot, message_event,
    ))
    records = (await plugin_materials.search(owner, MaterialSearchArguments())).records
    assert len(records) == 1 and records[0].name == "plan.txt"
    assert records[0].kind == "file"


@pytest.mark.integration
@pytest.mark.parametrize("message", ["", "   "])
async def test_empty_message_reply_comes_from_llm(
    app: App, chat_plugin, reply_chat: AsyncMock, message: str | Message,
) -> None:
    """真正的空消息通过校验回复模型提示缺少内容。"""

    event = group_event(message)
    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("模型生成的校验回复。")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()
    assert "INVALID_INPUT" in reply_chat.await_args.kwargs["instruction"]
    assert "消息内容" in reply_chat.await_args.kwargs["instruction"]


@pytest.mark.integration
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize(
    ("message", "field", "value", "fact"),
    [
        ("", None, None, "消息内容"),
        (Message(MessageSegment.image("file:///private.png")), None, None, "图片地址"),
        (Message(MessageSegment.image("https://example.invalid/a.png")),
         "chat_image_recognition_enabled", False, "图片识别"),
        (Message([MessageSegment.image("https://example.invalid/a.png")] * 3),
         "chat_max_images_per_message", 2, "2"),
    ],
)
async def test_natural_validation_uses_reply_llm_or_stays_silent(
    monkeypatch: pytest.MonkeyPatch, chat_plugin, reply_chat: AsyncMock,
    message: str | Message, field: str | None, value: object, fact: str, fails: bool,
) -> None:
    """校验提示只允许模型原文；模型失败时不得发送固定兜底。"""

    monkeypatch.setattr(chat_plugin.plugin_config, "chat_tts_enabled", True)
    if field is not None:
        monkeypatch.setattr(chat_plugin.plugin_config, field, value)
    if fails:
        reply_chat.side_effect = ChatAiUnavailableError("private upstream detail")
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            Mock(self_id="90001"), private_event(message), matcher=matcher,
            mention_handler=AsyncMock(return_value=None),
        )
    assert reply_chat.await_count == 1
    assert fact in reply_chat.await_args.kwargs["instruction"]
    assert "private upstream detail" not in reply_chat.await_args.kwargs["instruction"]
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()
    matcher.finish.assert_awaited_once_with()
    if fails:
        matcher.send.assert_not_awaited()
    else:
        matcher.send.assert_awaited_once_with(Message("模型生成的校验回复。"))


@pytest.mark.integration
@pytest.mark.parametrize(
    ("message", "max_input_chars", "subject"),
    [
        (" \t\n ", 40, "消息内容"),
        (Message([MessageSegment.at(90001), MessageSegment.text(" \t ")]), 1, "消息长度"),
        ("长" * 41, 40, "消息长度"),
        (" \t ", 1, "消息内容"),
        ("长" * 2, 1, "消息长度"),
    ],
)
async def test_validation_context_reaches_real_reply_service_and_model(
    monkeypatch: pytest.MonkeyPatch, chat_plugin,
    message: str | Message, max_input_chars: int, subject: str,
) -> None:
    """真实回复服务让空白和超限输入的校验回复到达底层模型，纯 At 按名称计长度。"""

    monkeypatch.setattr(chat_plugin, "_load_group_members", AsyncMock(return_value=[
        {"user_id": 90001, "nickname": "机器人昵称", "card": "桃桃"},
    ]))
    complete = AsyncMock(return_value="真实模型生成的校验回复。")
    service = ChatService(
        client=Mock(complete=complete),
        store=ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60),
        system_prompt="测试系统提示",
        max_input_chars=max_input_chars,
        cooldown_seconds=0,
        max_concurrent_requests=2,
        model_router=ContentModelRouter(
            text_model="chat-model", vision_model="vision-model",
            image_model="image-model", audio_model="audio-model",
        ),
    )
    monkeypatch.setattr(chat_plugin, "chat_service", service)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_max_input_chars", max_input_chars)
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            Mock(self_id="90001"), group_event(message), matcher=matcher,
            mention_handler=AsyncMock(return_value=None),
        )
    complete.assert_awaited_once()
    model_request = complete.await_args.kwargs
    prompt_context = model_request["prompt"].removeprefix("tester：")
    assert 0 < len(prompt_context.strip()) <= max_input_chars
    assert subject in model_request["messages"][1].content
    assert model_request["use_agent"] is False
    assert model_request["use_web_search"] is False
    matcher.send.assert_awaited_once_with(Message("真实模型生成的校验回复。"))
    matcher.finish.assert_awaited_once_with()
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()


@pytest.mark.integration
async def test_missing_model_reports_error_before_queue_or_model_call(
    monkeypatch, chat_plugin, reply_chat,
) -> None:
    """缺少模型直接返回配置错误，不创建队列任务或调用解释模型。"""
    from agent_bot.model_guard import ModelNotAvailableError

    guard = Mock(ensure_enabled=AsyncMock(side_effect=ModelNotAvailableError("AI_MODEL_NOT_FOUND")))
    monkeypatch.setattr(chat_plugin.chat_service, "_model_guard", guard)
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))

    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            Mock(self_id="90001"), private_event("画一只猫"), matcher=matcher,
            mention_handler=AsyncMock(return_value=None),
        )

    guard.ensure_enabled.assert_awaited_once()
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()
    reply_chat.assert_not_awaited()
    assert "模型未配置或未启用" in str(matcher.send.await_args.args[0])
    matcher.finish.assert_awaited_once_with()


@pytest.mark.integration
async def test_native_mention_preflight_uses_final_tools_and_chat_model(
    monkeypatch, chat_plugin, reply_chat,
) -> None:
    """原生 At 移除提醒工具后，关闭联网仍按 Agent 预检模型。"""
    from agent_bot.content_classifier import ContentCategory

    guard = Mock(ensure_enabled=AsyncMock())
    monkeypatch.setattr(chat_plugin.chat_service, "_model_guard", guard)
    monkeypatch.setattr(chat_plugin.chat_service, "_web_search_enabled", False)
    monkeypatch.setattr(chat_plugin._conversation_handler, "_available_tools_provider",
                        lambda _event: ("mention_member",))
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    mention = Message([MessageSegment.at(20002), MessageSegment.text("你好")])

    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            Mock(self_id="90001"), group_event("提醒他打招呼"), matcher=matcher,
            mention_handler=AsyncMock(return_value=mention),
        )

    guard.ensure_enabled.assert_awaited_once_with(
        chat_plugin.chat_service.model_router.model_for(ContentCategory.TEXT),
        capability="agent",
    )
    assert chat_plugin.conversation_queue.enqueue.await_args.args[0].chat_tools == ()
    reply_chat.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize("answer", ["甲乙丙丁戊", "甲乙丙丁戊己庚"])
async def test_model_answer_enters_gateway_once_without_rewriting_or_truncation_marker(
    monkeypatch: pytest.MonkeyPatch, chat_plugin, reply_chat: AsyncMock, answer: str,
) -> None:
    """原文经网关发送一次，超限改为完整文件，投递后才提交历史。"""

    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            Mock(self_id="90001"), private_event("普通问题"), matcher=matcher,
            mention_handler=AsyncMock(return_value=None),
        )
    job = chat_plugin.conversation_queue.enqueue.await_args.args[0]
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_max_reply_chars", 5)
    generate = AsyncMock(return_value=answer)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", generate)

    @asynccontextmanager
    async def tool_session(_job):
        """隔离外部工具服务，但保留真实队列生成和投递流程。"""
        yield (), None

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", tool_session)
    bot = AsyncMock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    gateway = chat_plugin._reply_gateway
    send = AsyncMock(wraps=gateway.send_bot_exact_part)
    send_file = AsyncMock(wraps=gateway.send_reply_file)
    monkeypatch.setattr(gateway, "send_bot_exact_part", send)
    monkeypatch.setattr(gateway, "send_reply_file", send_file)
    as_file = len(answer) > 5

    async def commit_after_send(*_args, **_kwargs):
        """历史提交必须发生在生成原文完成 OneBot 投递之后。"""
        if as_file:
            # 完整文件必须先上传至原私聊，不发送裁剪正文或额外提示。
            bot.call_api.assert_awaited_once()
            upload = bot.call_api.await_args
            assert upload.args == ("upload_private_file",)
            assert upload.kwargs["user_id"] == 10001
            assert upload.kwargs["name"].endswith(".txt")
            assert base64.b64decode(
                upload.kwargs["file"].removeprefix("base64://"),
            ).decode("utf-8") == answer
            bot.send_private_msg.assert_not_awaited()
        else:
            bot.send_private_msg.assert_awaited_once_with(user_id=10001, message=Message(answer))
            bot.call_api.assert_not_awaited()

    commit = AsyncMock(side_effect=commit_after_send)
    monkeypatch.setattr(chat_plugin.chat_service, "commit_queued", commit)
    processor = chat_plugin._ConversationProcessor()
    result = await processor.generate(job)
    assert result.reply_as_file is as_file
    assert await processor.delivery_count(job, result) == 1
    await processor.deliver_part(job, result, 0)
    await processor.commit(job, result)
    assert send.await_count + send_file.await_count == 1
    delivery = send_file if as_file else send
    assert isinstance(delivery.await_args.kwargs["reply"], GeneratedReply)
    assert delivery.await_args.kwargs["reply"].text == answer
    generate.assert_awaited_once()
    commit.assert_awaited_once()
    assert commit.await_args.args[2] == answer
    reply_chat.assert_not_awaited()


@pytest.mark.integration
async def test_chat_model_failure_explains_without_committing_failed_answer(
    monkeypatch: pytest.MonkeyPatch, chat_plugin, reply_chat: AsyncMock,
) -> None:
    """主模型失败只发送一次脱敏解释，不提交或重放失败回答与原工具。"""

    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            Mock(self_id="90001"), private_event("普通问题"), matcher=matcher,
            mention_handler=AsyncMock(return_value=None),
        )
    job = chat_plugin.conversation_queue.enqueue.await_args.args[0]
    generate = AsyncMock(side_effect=ChatAiUnavailableError(
        "private detail", error_code="AI_UPSTREAM_UNAVAILABLE",
    ))
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", generate)

    @asynccontextmanager
    async def tool_session(_job):
        """使用离线工具租约以便执行真实队列生成边界。"""
        yield (), None

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", tool_session)
    bot = AsyncMock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _bot_id: bot)
    commit = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "commit_queued", commit)
    processor = chat_plugin._ConversationProcessor()
    with pytest.raises(ChatAiUnavailableError) as error:
        await processor.generate(job)
    await processor.deliver_failure(job, error.value)
    bot.send_private_msg.assert_awaited_once_with(
        user_id=10001, message=Message("模型生成的校验回复。"),
    )
    assert len(bot.mock_calls) == 1
    commit.assert_not_awaited()
    generate.assert_awaited_once()
    reply_chat.assert_awaited_once()
    assert reply_chat.await_args.kwargs["use_agent"] is True
    instruction = reply_chat.await_args.kwargs["instruction"]
    assert "TASK_FAILED" in instruction and "ChatAiUnavailableError" in instruction
    assert "AI_UPSTREAM_UNAVAILABLE" in instruction and "private detail" not in instruction


@pytest.mark.integration
@pytest.mark.parametrize("error", [
    ChatBusyError("private"), ChatRateLimitedError(1),
    ConversationQueueFullError("private"), ConversationJobExpiredError("private"),
])
async def test_natural_queue_overload_finishes_without_composer(
    monkeypatch: pytest.MonkeyPatch, chat_plugin, reply_chat: AsyncMock, error: Exception,
) -> None:
    """队列入口的繁忙、限流和过期静默终止，不发送提示或增加模型负载。"""

    prompt = "普通问题"
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", AsyncMock(side_effect=error))
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            Mock(self_id="90001"), private_event(prompt), matcher=matcher,
            mention_handler=AsyncMock(return_value=None),
        )
    matcher.send.assert_not_awaited()
    matcher.finish.assert_awaited_once_with()
    reply_chat.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize(
    ("error", "fact"),
    [
        (ImageRecognitionDisabledError("secret"), "图片识别"),
        (TooManyImagesError("secret"), "图片数量"),
        (InvalidImageAttachmentError("secret"), "图片地址"),
        (TooManyVideosError("secret"), "视频数量"),
        (InvalidVideoAttachmentError("secret"), "视频地址"),
        (ForwardMessageUnavailableError("secret"), "读取引用记录"),
        (QuotedMessageEmptyError("secret"), "引用内容"),
        (QuotedPromptLimitError("secret"), "消息长度"),
    ],
)
async def test_quoted_validation_uses_typed_facts_without_fallback(
    monkeypatch: pytest.MonkeyPatch, chat_plugin, reply_chat: AsyncMock,
    error: Exception, fact: str, fails: bool,
) -> None:
    """引用内容校验错误只提供脱敏事实，生成失败则无 OneBot 文字。"""

    from agent_bot.integrations.onebot import conversation

    monkeypatch.setattr(conversation, "build_quoted_chat_input", AsyncMock(side_effect=error))
    if fails:
        reply_chat.side_effect = ChatAiUnavailableError("private")
    event = private_event("解释引用内容")
    event.reply = Reply(
        time=1, message_id=3, real_id=3, message_type="private",
        sender=Sender(user_id=10086), message=Message("引用"),
    )
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            Mock(self_id="90001"), event, matcher=matcher,
            mention_handler=AsyncMock(return_value=None),
        )
    assert reply_chat.await_count == 1
    assert fact in reply_chat.await_args.kwargs["instruction"]
    assert "secret" not in reply_chat.await_args.kwargs["instruction"]
    if fails:
        matcher.send.assert_not_awaited()
    else:
        matcher.send.assert_awaited_once_with(Message("模型生成的校验回复。"))
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()


def private_event(
    message: str | Message,
    *,
    user_id: int = 10001,
    nickname: str = "tester",
) -> PrivateMessageEvent:
    content = Message(message)
    return PrivateMessageEvent(
        time=1,
        self_id=90001,
        post_type="message",
        sub_type="friend",
        user_id=user_id,
        message_type="private",
        message_id=1,
        message=content,
        original_message=content,
        raw_message=str(content),
        font=0,
        sender=Sender(user_id=user_id, nickname=nickname),
        to_me=True,
    )


def group_event(
    message: str | Message,
    *,
    user_id: int = 10001,
    group_id: int = 20001,
    nickname: str = "tester",
    card: str | None = None,
    to_me: bool = True,
) -> GroupMessageEvent:
    content = Message(message)
    return GroupMessageEvent(
        time=1,
        self_id=90001,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=2,
        message=content,
        original_message=content,
        raw_message=str(content),
        font=0,
        sender=Sender(
            user_id=user_id,
            nickname=nickname,
            card=card,
            role="member",
        ),
        group_id=group_id,
        to_me=to_me,
    )


def mock_response(monkeypatch: pytest.MonkeyPatch, chat_plugin, answer: str) -> AsyncMock:
    """模拟队列模型结果，并经真实来源适配器与回复网关发送。"""

    respond_mock = AsyncMock()

    async def enqueue(job):
        """记录队列输入并只投递本测试指定的模型回答。"""

        await respond_mock(
            job.session_id,
            job.prompt,
            None,
            user=job.user,
            user_name=job.user_name,
            chat_context=job.chat_context,
            image_urls=job.image_urls,
            image_material_ids=job.image_material_ids,
            material_ids=job.material_ids,
            video_urls=job.video_urls,
            chat_tools=job.chat_tools,
            tool_session_token=None,
            recent_group_context=job.recent_group_context,
        )
        prefix = (
            MessageSegment.at(job.mention_target_id)
            if job.mention_target_id is not None
            else None
        )
        return await chat_plugin._reply_gateway.send_matcher(
            chat_plugin.natural_chat,
            generated_reply_from_model(answer, job.request_id),
            prefix=prefix,
        )

    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", AsyncMock(side_effect=enqueue))
    return respond_mock


@pytest.mark.integration
def test_draw_command_is_registered(chat_plugin) -> None:
    """显式画图命令必须拥有独立 Matcher，不能落入自然聊天。"""

    assert chat_plugin.draw_command is not None


@pytest.mark.integration
async def test_draw_command_sends_llm_text_then_submits_background_image(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """画图命令先答复再提交后台工作流，命令不等待图片生成。"""

    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_local_workflow_enabled",
        True,
    )
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_local_workflow_default_key",
        "portrait",
    )
    generate = AsyncMock(
        return_value=GeneratedWorkflowImage(media_type="image/png", data=b"image")
    )
    monkeypatch.setattr(
        chat_plugin,
        "workflow_service",
        type("WorkflowService", (), {"generate": generate})(),
    )
    accept = Mock(return_value="画一只猫")
    start = AsyncMock(return_value="我来画一只猫，稍等。")
    commit = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "accept_immediate", accept)
    monkeypatch.setattr(
        chat_plugin.chat_service,
        "generate_image_start_queued",
        start,
    )
    monkeypatch.setattr(chat_plugin.chat_service, "commit_queued", commit)
    enqueue = AsyncMock(return_value="1-0")
    monkeypatch.setattr(chat_plugin.image_queue, "enqueue", enqueue)
    event = private_event("/画图 画一只猫")

    async with app.test_matcher(chat_plugin.draw_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("我来画一只猫，稍等。"),
            bot=bot,
        )
        ctx.should_finished(chat_plugin.draw_command)

    generate.assert_not_awaited()
    task = enqueue.await_args.args[0]
    assert task.background_image
    assert task.workflow_request.workflow_key == "portrait"
    assert task.workflow_request.positive_prompt == "画一只猫"
    accept.assert_called_once()
    start.assert_awaited_once()
    commit.assert_awaited_once()
    assert commit.await_args.args[2] == "我来画一只猫，稍等。\n[绘图任务已提交]"


@pytest.mark.integration
def test_onebot_adapter_uses_required_access_token(chat_plugin) -> None:
    adapter = nonebot.get_adapter(Adapter)

    assert adapter.onebot_config.onebot_access_token == (
        "test-onebot-access-token-that-is-at-least-32-characters"
    )


@pytest.mark.integration
async def test_private_natural_chat_calls_service_and_sends_text_segment(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin
) -> None:
    """Verify private natural chat calls service and sends text segment."""

    respond = mock_response(monkeypatch, chat_plugin, "回答 [CQ:at,qq=all]")
    event = private_event("你好")

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("回答 [CQ:at,qq=all]")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    session_id, prompt = respond.await_args.args[:2]
    assert session_id.startswith("qq_")
    assert prompt == "你好"
    assert respond.await_args.kwargs["user"] is None
    assert respond.await_args.kwargs["user_name"] == "tester"
    context = respond.await_args.kwargs["chat_context"]
    assert context.model_dump(by_alias=True) == {
        "platform": "qq",
        "scene": "private",
        "groupId": None,
        "groupName": None,
        "senderId": "10001",
        "senderName": "tester",
    }


@pytest.mark.integration
@pytest.mark.parametrize(
    ("local_enabled", "generation_enabled"), [(False, False), (False, True), (True, True)],
)
@pytest.mark.parametrize("prompt", [
    "自拍一个", "桃桃，帮我画一张猫咪", "不要画一张图",
    "解释生成图片是什么意思", "前面是一大段聊天背景。" * 5 + "画一张猫", "你好",
])
async def test_natural_chat_leaves_drawing_selection_to_agent(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    prompt: str,
    local_enabled: bool,
    generation_enabled: bool,
) -> None:
    """自然消息始终进入对话队列，仅提供已授权工具，不按绘图词强制分流。"""

    enqueue = AsyncMock(return_value="1-0")
    image_enqueue = AsyncMock()
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", enqueue)
    monkeypatch.setattr(chat_plugin.image_queue, "enqueue", image_enqueue)
    monkeypatch.setattr(
        chat_plugin.plugin_config, "chat_local_workflow_enabled", local_enabled,
    )
    monkeypatch.setattr(
        chat_plugin.plugin_config, "chat_image_generation_enabled", generation_enabled,
    )
    authorized_tools = (
        ("local_workflow_list", "local_workflow_generate_image") if local_enabled else ()
    )
    monkeypatch.setattr(
        chat_plugin._tool_session_manager, "available_tools", lambda _event: authorized_tools,
    )
    event = private_event(prompt)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_finished(chat_plugin.natural_chat)

    enqueue.assert_awaited_once()
    job = enqueue.await_args.args[0]
    assert job.prompt == prompt
    assert job.media_output is None
    assert not job.background_image
    assert job.workflow_request is None
    assert job.chat_tools == authorized_tools
    image_enqueue.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize("prompt", [
    "请用语音回答我，打个招呼", "桃桃，来句语音我听听", "你好",
    "介绍一下语音回复功能", "前面是一大段聊天背景。" * 5 + "让我听听你的声音",
])
async def test_chat_enqueues_authorized_tts_for_model_selection(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    prompt: str,
) -> None:
    """所有聊天都保留授权的语音工具，由模型决定是否调用。"""

    enqueue = AsyncMock(return_value="1-0")
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", enqueue)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_tts_enabled", True)
    monkeypatch.setattr(
        chat_plugin._tool_session_manager,
        "available_tools",
        lambda _event: ("tts_generate",),
    )
    event = private_event(prompt)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_finished(chat_plugin.natural_chat)

    job = enqueue.await_args.args[0]
    assert job.media_output is None
    assert job.chat_tools == ("tts_generate",)


@pytest.mark.integration
async def test_local_image_request_routes_to_agent_tools_without_cloud_media(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """仅授权工具不提前发送开画提示，实际调用绘图工具时才发送。"""

    respond = mock_response(monkeypatch, chat_plugin, "工作流已生成图片。")
    start = AsyncMock(return_value="好啦，我开始画了。")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_image_start_queued", start)
    respond_media = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "respond_media", respond_media)
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_image_generation_enabled",
        True,
    )
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_local_workflow_enabled",
        True,
    )
    monkeypatch.setattr(
        chat_plugin._tool_session_manager,
        "available_tools",
        lambda _event: (
            "naraka_query",
            "mention_member",
            "local_workflow_list",
            "local_workflow_generate_image",
        ),
    )
    event = group_event("桃桃，帮我画两张猫咪方案")

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("工作流已生成图片。")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    start.assert_not_awaited()
    assert respond.await_args.kwargs["chat_tools"] == (
        "mention_member",
        "local_workflow_list",
        "local_workflow_generate_image",
    )
    respond_media.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize("with_image", [False, True])
async def test_local_image_request_enqueues_without_additional_start_reply(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    with_image,
) -> None:
    """自然语言生图入队阶段不抢先发送，开画答复由工作线程处理。"""

    start = AsyncMock(return_value="好啦，我开始画了。")
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_chunk_chars", 3)
    enqueue = AsyncMock(return_value="1-0")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_image_start_queued", start)
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", enqueue)
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_image_generation_enabled",
        True,
    )
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_local_workflow_enabled",
        True,
    )
    monkeypatch.setattr(
        chat_plugin._tool_session_manager,
        "available_tools",
        lambda _event: (
            "local_workflow_list",
            "local_workflow_generate_image",
        ),
    )
    event = group_event("桃桃，帮我画一张猫咪", user_id=10086)
    if with_image:
        event.message += MessageSegment.image("https://gchat.qpic.cn/source")
        event.original_message = event.message

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_finished(chat_plugin.natural_chat)

    start.assert_not_awaited()
    job = enqueue.await_args.args[0]
    assert job.user_id == "10086"
    assert job.image_urls == ()
    assert len(job.image_material_ids) == int(with_image)
    assert job.material_ids == job.image_material_ids
    assert job.image_start_reply is None
    assert not job.image_start_parts
    assert job.chat_tools == (
        "local_workflow_list",
        "local_workflow_generate_image",
    )


@pytest.mark.integration
@pytest.mark.parametrize("local_enabled", [False, True])
@pytest.mark.parametrize("with_video", [False, True])
async def test_drawing_words_with_attached_media_preserve_chat_input(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    local_enabled: bool,
    with_video: bool,
) -> None:
    """绘图措辞不拒绝参考媒体，模型仍收到原始提示和图片或视频。"""

    enqueue = AsyncMock(return_value="1-0")
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", enqueue)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_image_generation_enabled", True)
    monkeypatch.setattr(
        chat_plugin.plugin_config, "chat_local_workflow_enabled", local_enabled,
    )
    prompt = "请生成一张猫咪图片"
    media = (
        MessageSegment.video("https://example.qq.com/a.mp4") if with_video
        else MessageSegment.image("https://gchat.qpic.cn/source")
    )
    event = private_event(
        Message([
            media,
            MessageSegment.text(prompt),
        ])
    )

    if with_video:
        event = private_event(prompt)
        event.reply = Reply(
            time=1, message_type="private", message_id=87, real_id=87,
            sender=Sender(user_id=10086, nickname="参考素材发送者"),
            message=Message(media),
        )

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_finished(chat_plugin.natural_chat)

    enqueue.assert_awaited_once()
    job = enqueue.await_args.args[0]
    assert prompt in job.prompt
    assert job.image_urls == ()
    assert len(job.image_material_ids) == int(not with_video)
    assert job.material_ids == job.image_material_ids
    assert job.video_urls == (("https://example.qq.com/a.mp4",) if with_video else ())
    assert job.media_output is None


@pytest.mark.integration
async def test_targeted_group_drawing_request_uses_chat_queue(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """群聊明确要求生图也交给模型判断，不直接创建云端媒体任务。"""

    enqueue = AsyncMock(return_value="1-0")
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", enqueue)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_image_generation_enabled", True)
    event = group_event("帮我生成一张猫咪的图片", to_me=True)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_finished(chat_plugin.natural_chat)

    enqueue.assert_awaited_once()
    job = enqueue.await_args.args[0]
    assert job.scene == "group"
    assert job.media_output is None
    assert job.prompt == "帮我生成一张猫咪的图片"


@pytest.mark.integration
async def test_untargeted_group_image_request_does_not_trigger(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    respond = AsyncMock()
    respond_media = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "respond", respond)
    monkeypatch.setattr(chat_plugin.chat_service, "respond_media", respond_media)
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_image_generation_enabled",
        True,
    )
    event = group_event("帮我生成一张猫咪的图片", to_me=False)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.natural_chat)

    respond_media.assert_not_awaited()
    respond.assert_not_awaited()


@pytest.mark.integration
async def test_ordinary_chat_stays_text_when_tts_is_enabled(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify ordinary chat stays text when tts is enabled."""

    mock_response(monkeypatch, chat_plugin, "普通文字回答")
    respond_media = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "respond_media", respond_media)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_tts_enabled", True)
    event = private_event("介绍一下语音回复功能")

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("普通文字回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond_media.assert_not_awaited()


@pytest.mark.integration
async def test_chat_without_tts_authorization_still_enqueues_text(
    monkeypatch: pytest.MonkeyPatch, chat_plugin,
) -> None:
    """未授权语音时只取消工具能力，不用正则拦截用户消息。"""
    enqueue = AsyncMock(return_value="1-0")
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", enqueue)
    monkeypatch.setattr(chat_plugin._tool_session_manager, "available_tools", lambda _event: ())
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            Mock(self_id="90001"), private_event("语音回复：请打个招呼"),
            matcher=matcher, mention_handler=AsyncMock(return_value=None),
        )
    job = enqueue.await_args.args[0]
    assert job.chat_tools == ()
    assert job.media_output is None
    matcher.send.assert_not_awaited()


@pytest.mark.integration
async def test_tts_limit_applies_to_generated_speech_not_user_prompt(
    monkeypatch: pytest.MonkeyPatch, chat_plugin,
) -> None:
    """较长问题可以由模型生成短语音，不能用 TTS 文本上限拦截原问题。"""
    enqueue = AsyncMock(return_value="1-0")
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", enqueue)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_tts_max_input_chars", 20)
    monkeypatch.setattr(
        chat_plugin._tool_session_manager, "available_tools", lambda _event: ("tts_generate",),
    )
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._conversation_handler.natural_chat(
            Mock(self_id="90001"), private_event("请用语音回答我，" + "长" * 20),
            matcher=matcher, mention_handler=AsyncMock(return_value=None),
        )
    job = enqueue.await_args.args[0]
    assert job.chat_tools == ("tts_generate",)
    assert len(job.prompt) > 20
    matcher.send.assert_not_awaited()


@pytest.mark.integration
async def test_media_delivery_preserves_reply_prefix(chat_plugin) -> None:
    matcher = Mock()
    matcher.send = AsyncMock()
    prefix = MessageSegment.at(10086)
    response = ChatMediaResponse(
        items=(
            ChatMediaItem(type="AUDIO", mimeType="audio/mpeg", data="SUQz"),
        )
    )

    delivered = await chat_plugin._deliver_media_answer(
        matcher,
        response,
        reply_prefix=prefix,
    )

    matcher.send.assert_awaited_once_with(
        Message([prefix, MessageSegment.record("base64://SUQz")])
    )
    assert delivered == "[已生成语音]"


@pytest.mark.integration
async def test_natural_chat_forwards_sanitized_nickname_only_when_enabled(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify natural chat forwards sanitized nickname only when enabled."""

    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_llm_forward_user_name",
        True,
    )
    respond = mock_response(monkeypatch, chat_plugin, "回答")
    event = private_event(
        "你好",
        nickname="\x00  当前\nQQ\t昵称  ",
    )

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    assert respond.await_args.kwargs["user"] == "当前 QQ 昵称"
    assert respond.await_args.kwargs["user_name"] == "当前 QQ 昵称"


@pytest.mark.integration
async def test_natural_chat_forwards_multiple_qq_images_as_current_materials(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify natural chat forwards multiple qq images as current materials."""

    respond = mock_response(monkeypatch, chat_plugin, "识别回答")
    message = Message(
        [
            MessageSegment.text("比较这两张图"),
            MessageSegment(
                type="image",
                data={"url": "https://gchat.qpic.cn/first.jpg?token=one"},
            ),
            MessageSegment(
                type="image",
                data={"url": "https://multimedia.nt.qq.com.cn/second.png?token=two"},
            ),
        ]
    )
    event = private_event(message)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("识别回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    assert "比较这两张图" in respond.await_args.args[1]
    assert respond.await_args.kwargs["image_urls"] == ()
    ids = respond.await_args.kwargs["image_material_ids"]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert all(id in respond.await_args.args[1] for id in ids)


@pytest.mark.integration
async def test_natural_chat_includes_all_quoted_content_in_current_request(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """当前模型请求保留引用，但长期记忆证据只绑定当前发言人的原始文本。"""

    respond = mock_response(monkeypatch, chat_plugin, "引用回答")
    event = private_event("这段内容是什么意思？")
    event.reply = Reply(
        time=1,
        message_type="private",
        message_id=87,
        real_id=87,
        sender=Sender(user_id=10086, nickname="被引用者"),
        message=Message(
            [
                MessageSegment.text("引用正文"),
                MessageSegment(
                    type="image",
                    data={"url": "https://gchat.qpic.cn/quoted-chat.jpg"},
                ),
                MessageSegment(
                    type="video",
                    data={
                        "url": "https://gzc-download.ftn.qq.com/quoted-chat.mp4?token=one",
                        "file_size": 8_388_608,
                    },
                ),
            ]
        ),
    )

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("引用回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    assert "被引用者" in respond.await_args.args[1]
    assert "引用正文" in respond.await_args.args[1]
    assert "这段内容是什么意思？" in respond.await_args.args[1]
    queued = chat_plugin.conversation_queue.enqueue.await_args.args[0]
    assert queued.memory_source_text == "这段内容是什么意思？"
    assert "引用正文" not in queued.memory_source_text
    assert "引用正文" in queued.memory_quoted_source.text
    assert queued.memory_quoted_source.text in queued.prompt
    assert queued.memory_quoted_source.event_id == "qq:90001:87"
    assert queued.memory_quoted_source.user_id == "10086"
    assert respond.await_args.kwargs["image_urls"] == ()
    ids = respond.await_args.kwargs["image_material_ids"]
    assert len(ids) == 1 and ids[0] in respond.await_args.args[1]
    assert respond.await_args.kwargs["video_urls"] == (
        "https://gzc-download.ftn.qq.com/quoted-chat.mp4?token=one",
    )


@pytest.mark.integration
async def test_summary_command_summarizes_current_reply_with_image_and_video(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """引用图文和视频的总结直接经网关发送主模型原文。"""

    ai_summary = AsyncMock(return_value="引用消息总结")
    monkeypatch.setattr(chat_plugin.chat_service, "ask_once", ai_summary)
    event = private_event("/总结")
    event.reply = Reply(
        time=1,
        message_type="private",
        message_id=88,
        real_id=88,
        sender=Sender(user_id=10086, nickname="被引用者"),
        message=Message(
            [
                MessageSegment.text("需要总结的正文"),
                MessageSegment(
                    type="image",
                    data={"url": "https://gchat.qpic.cn/quoted-summary.jpg"},
                ),
                MessageSegment(
                    type="video",
                    data={
                        "url": "https://gzc-download.ftn.qq.com/quoted-summary.mp4",
                        "file_size": 8_388_608,
                    },
                ),
            ]
        ),
    )

    async with app.test_matcher(chat_plugin.summary_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message("引用消息总结"), bot=bot)
        ctx.should_finished(chat_plugin.summary_command)

    ai_summary.assert_awaited_once()
    assert "需要总结的正文" in ai_summary.await_args.args[1]
    assert ai_summary.await_args.kwargs["image_urls"] == ()
    ids = ai_summary.await_args.kwargs["image_material_ids"]
    assert len(ids) == 1 and ids[0] in ai_summary.await_args.args[1]
    assert ai_summary.await_args.kwargs["video_urls"] == (
        "https://gzc-download.ftn.qq.com/quoted-summary.mp4",
    )
    assert ai_summary.await_args.kwargs["system_prompt"] == (
        chat_plugin.plugin_config.chat_system_prompt
    )
    assert ai_summary.await_args.kwargs["user_name"] == "tester"


@pytest.mark.integration
async def test_summary_command_expands_quoted_forward_message(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """展开转发内容后仅生成并投递一次模型总结。"""

    ai_summary = AsyncMock(return_value="合并记录总结")
    monkeypatch.setattr(chat_plugin.chat_service, "ask_once", ai_summary)
    event = group_event("/总结")
    event.reply = Reply(
        time=1,
        message_type="group",
        message_id=99,
        real_id=99,
        sender=Sender(user_id=10086, nickname="转发者"),
        message=Message(
            MessageSegment(type="forward", data={"id": "forward-message-id"})
        ),
    )

    async with app.test_matcher(chat_plugin.summary_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_api(
            "get_forward_msg",
            {"message_id": "forward-message-id"},
            result={
                "messages": [
                    {
                        "sender": {"nickname": "群友甲", "user_id": "10010"},
                        "message": [
                            {"type": "text", "data": {"text": "合并记录正文"}},
                        ],
                    }
                ]
            },
        )
        ctx.should_call_send(event, Message("合并记录总结"), bot=bot)
        ctx.should_finished(chat_plugin.summary_command)

    assert "群友甲" in ai_summary.await_args.args[1]
    assert "合并记录正文" in ai_summary.await_args.args[1]


@pytest.mark.integration
async def test_summary_command_requires_current_reply(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """未引用消息时通过 Composer 给出命令用法。"""

    ai_summary = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "ask_once", ai_summary)
    event = private_event("/总结")

    async with app.test_matcher(chat_plugin.summary_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message("模型生成的校验回复。"),
            bot=bot,
        )
        ctx.should_finished(chat_plugin.summary_command)

    ai_summary.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize(("error", "kind", "fact"), [
    (ImageRecognitionDisabledError(), "FEATURE_UNAVAILABLE", "图片识别"),
    (TooManyImagesError(), "INVALID_INPUT", "引用图片"),
    (InvalidImageAttachmentError(), "INVALID_INPUT", "图片地址"),
    (InvalidVideoAttachmentError(), "INVALID_INPUT", "视频地址"),
    (TooManyVideosError(), "INVALID_INPUT", "引用视频"),
    (ForwardMessageUnavailableError(), "TASK_FAILED", "读取合并聊天记录"),
    (QuotedMessageEmptyError(), "INVALID_INPUT", "引用消息"),
    (QuotedPromptLimitError(), "FEATURE_UNAVAILABLE", "总结输入长度"),
])
async def test_summary_validation_uses_typed_composer(
    monkeypatch, chat_plugin, reply_chat, error, kind, fact,
) -> None:
    """所有引用校验错误都必须由 Composer 表述并保持异常脱敏。"""

    monkeypatch.setattr(
        "agent_bot.integrations.onebot.summary.build_quoted_summary_input",
        AsyncMock(side_effect=error),
    )
    event = private_event("/总结")
    event.reply = Reply(
        time=1, message_type="private", message_id=2, real_id=2,
        sender=Sender(user_id=10002, nickname="引用用户"), message=Message("原文"),
    )
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._summary_handler.handle(Mock(self_id="90001"), event, matcher=matcher)
    matcher.send.assert_awaited_once_with(Message("模型生成的校验回复。"))
    matcher.finish.assert_awaited_once_with()
    instruction = reply_chat.await_args.kwargs["instruction"]
    assert kind in instruction
    assert fact in instruction


@pytest.mark.integration
@pytest.mark.parametrize("failure", [
    ChatAiUnavailableError("private detail"), ChatBusyError(), ChatRateLimitedError(1),
    ConversationQueueFullError(), None,
])
async def test_summary_model_failure_or_blank_is_silent(
    monkeypatch, chat_plugin, reply_chat, failure,
) -> None:
    """主总结模型失败或空白时禁止生成固定兜底和二次解释。"""

    ask = AsyncMock(side_effect=failure, return_value="  ")
    monkeypatch.setattr(chat_plugin.chat_service, "ask_once", ask)
    event = private_event("/总结")
    event.reply = Reply(
        time=1, message_type="private", message_id=2, real_id=2,
        sender=Sender(user_id=10002, nickname="引用用户"), message=Message("原文"),
    )
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._summary_handler.handle(Mock(self_id="90001"), event, matcher=matcher)
    matcher.send.assert_not_awaited()
    matcher.finish.assert_awaited_once_with()
    reply_chat.assert_not_awaited()


@pytest.mark.integration
async def test_summary_model_output_is_adapted_once_and_sent_without_rewriting(
    monkeypatch, chat_plugin, reply_chat,
) -> None:
    """已有总结仅适配一次来源并由网关裁剪，不添加截断标记或二次改写。"""

    monkeypatch.setattr(
        chat_plugin.chat_service, "ask_once", AsyncMock(return_value="甲乙丙丁戊己"),
    )
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_max_reply_chars", 5)
    gateway = chat_plugin._reply_gateway
    send = AsyncMock(wraps=gateway.send_matcher)
    monkeypatch.setattr(gateway, "send_matcher", send)
    event = private_event("/总结")
    event.reply = Reply(
        time=1, message_type="private", message_id=2, real_id=2,
        sender=Sender(user_id=10002, nickname="引用用户"), message=Message("原文"),
    )
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._summary_handler.handle(Mock(self_id="90001"), event, matcher=matcher)
    send.assert_awaited_once()
    generated = send.await_args.args[1]
    assert isinstance(generated, GeneratedReply)
    assert generated.text == "甲乙丙丁戊己"
    matcher.send.assert_awaited_once_with(Message("甲乙丙丁戊"))
    reply_chat.assert_not_awaited()


@pytest.mark.integration
async def test_group_natural_chat_requires_to_me(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin
) -> None:
    """Verify group natural chat requires to me."""

    respond = mock_response(monkeypatch, chat_plugin, "群聊回答")

    ignored_event = group_event("没有艾特", to_me=False)
    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, ignored_event)
        ctx.should_not_pass_rule(chat_plugin.natural_chat)
    respond.assert_not_awaited()

    event = group_event("群聊问题", to_me=True)
    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("群聊回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    assert respond.await_args.args[1] == "群聊问题"


@pytest.mark.integration
async def test_group_natural_chat_replies_to_bot_only_mention(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """纯 At 将机器人群名片作为普通聊天提示词，不能生成校验回复指令。"""

    monkeypatch.setattr(chat_plugin, "_load_group_members", AsyncMock(return_value=[
        {"user_id": 90001, "nickname": "机器人昵称", "card": "桃桃"},
    ]))
    respond = mock_response(monkeypatch, chat_plugin, "找我有什么事呀？")
    event = group_event(Message(MessageSegment.at(90001)), to_me=True)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("找我有什么事呀？")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    assert respond.await_args.args[1] == "桃桃"
    chat_plugin.chat_service.generate_reply_queued.assert_not_awaited()


@pytest.mark.integration
async def test_group_natural_chat_triggers_when_message_contains_configured_nickname(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify group natural chat triggers when message contains configured nickname."""

    monkeypatch.setattr(chat_plugin.plugin_config, "nickname", {"小派"})
    respond = mock_response(monkeypatch, chat_plugin, "群聊回答")
    event = group_event("大家问一下小派今天天气", to_me=False)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("群聊回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    assert respond.await_args.args[1] == "大家问一下小派今天天气"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("nickname", "original_message", "adapter_message"),
    [
        ("桃桃", "桃桃，帮我看看", "帮我看看"),
        ("桃桃", "桃桃，桃桃再说一次", "桃桃再说一次"),
        ("Dispatch", "DISPATCH, help me", "help me"),
    ],
)
async def test_group_nickname_trigger_preserves_adapter_removed_nickname(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    nickname: str,
    original_message: str,
    adapter_message: str,
) -> None:
    """昵称被 OneBot 适配器用于触发后，传给模型的消息仍保留完整昵称。"""

    monkeypatch.setattr(chat_plugin.plugin_config, "nickname", {nickname})
    respond = mock_response(monkeypatch, chat_plugin, "群聊回答")
    event = group_event(original_message, to_me=True)
    event.message = Message(adapter_message)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("群聊回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    assert respond.await_args.args[1] == original_message


@pytest.mark.integration
async def test_group_nickname_trigger_preserves_original_after_runtime_nickname_refresh(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """适配器与热刷新配置昵称不一致时，仍以 OneBot 保存的原始消息为准。"""

    monkeypatch.setattr(chat_plugin.plugin_config, "nickname", {"新昵称"})
    respond = mock_response(monkeypatch, chat_plugin, "群聊回答")
    event = group_event("桃桃，帮我看看", to_me=True)
    event.message = Message("帮我看看")

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("群聊回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    assert respond.await_args.args[1] == "桃桃，帮我看看"


@pytest.mark.integration
async def test_group_nickname_trigger_ignores_english_case(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify group nickname trigger ignores english case."""

    monkeypatch.setattr(chat_plugin.plugin_config, "nickname", {"Dispatch"})
    respond = mock_response(monkeypatch, chat_plugin, "群聊回答")
    event = group_event("大家问一下DISPATCH今天天气", to_me=False)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("群聊回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()


@pytest.mark.integration
async def test_group_nickname_trigger_trims_configured_name(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify group nickname trigger trims configured name."""

    monkeypatch.setattr(chat_plugin.plugin_config, "nickname", {" Dispatch "})
    respond = mock_response(monkeypatch, chat_plugin, "群聊回答")
    event = group_event("大家问一下DISPATCH今天天气", to_me=False)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("群聊回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()


@pytest.mark.integration
@pytest.mark.parametrize("nickname", ["", "   "])
async def test_group_nickname_trigger_ignores_empty_configured_name(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    nickname: str,
) -> None:
    monkeypatch.setattr(chat_plugin.plugin_config, "nickname", {nickname})
    respond = mock_response(monkeypatch, chat_plugin, "不应发送")
    event = group_event("普通   群消息", to_me=False)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.natural_chat)

    respond.assert_not_awaited()


@pytest.mark.integration
async def test_group_natural_chat_passes_group_and_sender_context(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify group natural chat passes group and sender context."""

    respond = mock_response(monkeypatch, chat_plugin, "群聊回答")
    resolve_group_name = AsyncMock(return_value="测试交流群")
    monkeypatch.setattr(chat_plugin, "_resolve_group_name", resolve_group_name)
    event = group_event(
        "我在哪个群？",
        user_id=10001,
        group_id=20001,
        nickname="QQ 昵称",
        card="群名片",
    )

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("群聊回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    context = respond.await_args.kwargs["chat_context"]
    assert context.model_dump(by_alias=True) == {
        "platform": "qq",
        "scene": "group",
        "groupId": "20001",
        "groupName": "测试交流群",
        "senderId": "10001",
        "senderName": "群名片",
    }


@pytest.mark.integration
async def test_group_natural_chat_reads_prior_group_text_without_persisting_current_twice(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify group natural chat reads prior group text without persisting current twice."""

    respond = mock_response(monkeypatch, chat_plugin, "十点开会")
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_group_recent_context_enabled",
        True,
    )
    store = RecentGroupContextStore(
        message_limit=10,
        max_chars=4000,
        ttl_seconds=300,
        max_groups=10,
    )
    store.record(
        bot_id="90001",
        group_id="20001",
        sender_id="10002",
        sender_name="小明",
        content="明天十点开会",
    )
    store.record(
        bot_id="90001",
        group_id="20002",
        sender_id="10003",
        sender_name="其他群成员",
        content="不应跨群读取",
    )
    monkeypatch.setattr(chat_plugin, "group_recent_context_store", store)

    async def interleaved_mention_parse(bot, event):
        store.record(
            bot_id="90001",
            group_id="20001",
            sender_id="10004",
            sender_name="后来发言的人",
            content="本条触发消息之后才出现",
        )
        return None

    monkeypatch.setattr(
        chat_plugin,
        "_handle_conversational_mention",
        interleaved_mention_parse,
    )
    event = group_event("几点开会？", group_id=20001)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("十点开会")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    recent_context = respond.await_args.kwargs["recent_group_context"]
    assert recent_context == "小明(QQ 10002)：明天十点开会"
    assert "几点开会" not in recent_context
    assert "不应跨群读取" not in recent_context
    assert "本条触发消息之后才出现" not in recent_context
    assert store.context(bot_id="90001", group_id="20001") == (
        "小明(QQ 10002)：明天十点开会\n"
        "tester(QQ 10001)：几点开会？\n"
        "后来发言的人(QQ 10004)：本条触发消息之后才出现"
    )


@pytest.mark.integration
async def test_passive_plain_group_text_is_recorded_for_later_context(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_group_recent_context_enabled",
        True,
    )
    class ThreadRecordingGroupStore(RecentGroupContextStore):
        def __init__(self) -> None:
            super().__init__(
                message_limit=10,
                max_chars=4000,
                ttl_seconds=300,
                max_groups=10,
            )
            self.record_threads: list[int] = []

        def record(self, **kwargs: object) -> None:
            self.record_threads.append(threading.get_ident())
            super().record(**kwargs)

    store = ThreadRecordingGroupStore()
    event_loop_thread = threading.get_ident()
    monkeypatch.setattr(chat_plugin, "group_recent_context_store", store)
    auto_speak = Mock()
    auto_speak.accepts_message.side_effect = (
        lambda bot_id, group_id: bot_id == "90001" and group_id == "20001"
    )
    auto_speak.handle_group_message = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_plugin, "auto_speak_service", auto_speak)
    event = group_event(
        "  明天\n十点开会  ",
        user_id=10002,
        nickname="小明",
        card="技术部小明",
        to_me=False,
    )

    async with app.test_matcher(chat_plugin.group_recent_context) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)

    assert store.context(bot_id="90001", group_id="20001") == (
        "技术部小明(QQ 10002)：明天 十点开会"
    )
    assert store.record_threads
    assert all(thread_id != event_loop_thread for thread_id in store.record_threads)
    auto_speak.handle_group_message.assert_awaited_once()
    assert auto_speak.handle_group_message.await_args.args[1:] == (
        "20001",
        "技术部小明(QQ 10002)：明天 十点开会",
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    ("event", "recorded_group"),
    [
        (group_event("其他群普通消息", group_id=20002, to_me=False), "20002"),
        (group_event("机器人自己的回复", user_id=90001, to_me=False), None),
    ],
)
async def test_message_auto_speak_skips_non_target_and_self_messages(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    event: GroupMessageEvent,
    recorded_group: str | None,
) -> None:
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_group_recent_context_enabled",
        True,
    )
    store = RecentGroupContextStore(
        message_limit=10,
        max_chars=4000,
        ttl_seconds=300,
        max_groups=10,
    )
    monkeypatch.setattr(chat_plugin, "group_recent_context_store", store)
    auto_speak = Mock()
    auto_speak.accepts_message.side_effect = (
        lambda bot_id, group_id: bot_id == "90001" and group_id == "20001"
    )
    auto_speak.handle_group_message = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_plugin, "auto_speak_service", auto_speak)

    async with app.test_matcher(chat_plugin.group_recent_context) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)

    auto_speak.handle_group_message.assert_not_awaited()
    if recorded_group is None:
        assert store.context(bot_id="90001", group_id="20001") is None
    else:
        assert store.context(bot_id="90001", group_id=recorded_group) is not None


@pytest.mark.integration
async def test_private_message_never_enters_message_auto_speak_matcher(
    app: App,
    chat_plugin,
) -> None:
    event = private_event("普通私聊消息")

    async with app.test_matcher(chat_plugin.group_recent_context) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.group_recent_context)


@pytest.mark.integration
async def test_passive_group_memory_failure_does_not_fail_message_handling(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_group_recent_context_enabled",
        True,
    )

    class FailingGroupStore:
        def record(self, **_kwargs: object) -> None:
            raise ChatMemoryStoreError("database details must not escape")

    monkeypatch.setattr(chat_plugin, "group_recent_context_store", FailingGroupStore())
    event = group_event("不会触发机器人回复的普通群消息", to_me=False)

    async with app.test_matcher(chat_plugin.group_recent_context) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)


@pytest.mark.integration
@pytest.mark.parametrize(
    "message",
    [
        "/unknown 不应记录",
        Message(
            [
                MessageSegment.text("图片消息"),
                MessageSegment.image("https://example.com/image.png"),
            ]
        ),
    ],
)
async def test_recent_group_context_ignores_commands_and_non_text_messages(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    message: str | Message,
) -> None:
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_group_recent_context_enabled",
        True,
    )
    store = RecentGroupContextStore(
        message_limit=10,
        max_chars=4000,
        ttl_seconds=300,
        max_groups=10,
    )
    monkeypatch.setattr(chat_plugin, "group_recent_context_store", store)
    auto_speak = Mock()
    auto_speak.accepts_message.return_value = True
    auto_speak.handle_group_message = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_plugin, "auto_speak_service", auto_speak)
    event = group_event(message, to_me=False)

    async with app.test_matcher(chat_plugin.group_recent_context) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.group_recent_context)

    assert store.context(bot_id="90001", group_id="20001") is None
    auto_speak.handle_group_message.assert_not_awaited()


@pytest.mark.integration
async def test_recent_group_context_ignores_plain_text_reply(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_group_recent_context_enabled",
        True,
    )
    store = RecentGroupContextStore(
        message_limit=10,
        max_chars=4000,
        ttl_seconds=300,
        max_groups=10,
    )
    monkeypatch.setattr(chat_plugin, "group_recent_context_store", store)
    auto_speak = Mock()
    auto_speak.accepts_message.return_value = True
    auto_speak.handle_group_message = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_plugin, "auto_speak_service", auto_speak)
    event = group_event("回复正文", to_me=False)
    event.reply = Reply(
        time=1,
        message_type="group",
        message_id=99,
        real_id=99,
        sender=Sender(user_id=10086, nickname="被引用者"),
        message=Message("被引用内容"),
    )

    async with app.test_matcher(chat_plugin.group_recent_context) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.group_recent_context)

    assert store.context(bot_id="90001", group_id="20001") is None
    auto_speak.handle_group_message.assert_not_awaited()


@pytest.mark.integration
async def test_recent_group_context_does_not_collect_current_nickname_trigger(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_group_recent_context_enabled",
        True,
    )
    monkeypatch.setattr(chat_plugin.plugin_config, "nickname", {"桃桃"})
    auto_speak = Mock()
    auto_speak.accepts_message.return_value = True
    auto_speak.handle_group_message = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_plugin, "auto_speak_service", auto_speak)
    event = group_event("桃桃，几点开会？", to_me=False)

    async with app.test_matcher(chat_plugin.group_recent_context) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.group_recent_context)

    auto_speak.handle_group_message.assert_not_awaited()


@pytest.mark.integration
async def test_recent_group_context_does_not_trigger_on_native_bot_mention(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_group_recent_context_enabled",
        True,
    )
    auto_speak = Mock()
    auto_speak.accepts_message.return_value = True
    auto_speak.handle_group_message = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_plugin, "auto_speak_service", auto_speak)
    event = group_event(
        Message([MessageSegment.at(90001), MessageSegment.text(" 你好")]),
        to_me=True,
    )

    async with app.test_matcher(chat_plugin.group_recent_context) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.group_recent_context)

    auto_speak.handle_group_message.assert_not_awaited()


@pytest.mark.integration
async def test_recent_group_context_does_not_record_bot_self_messages(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_group_recent_context_enabled",
        True,
    )
    store = RecentGroupContextStore(
        message_limit=10,
        max_chars=4000,
        ttl_seconds=300,
        max_groups=10,
    )
    monkeypatch.setattr(chat_plugin, "group_recent_context_store", store)
    event = group_event("机器人自己的回复", user_id=90001, to_me=False)

    async with app.test_matcher(chat_plugin.group_recent_context) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)

    assert store.context(bot_id="90001", group_id="20001") is None


@pytest.mark.integration
async def test_group_natural_chat_keeps_ids_when_group_info_is_unavailable(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify group natural chat keeps ids when group info is unavailable."""

    respond = mock_response(monkeypatch, chat_plugin, "降级回答")
    event = group_event("现在在哪里？", group_id=20001)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("降级回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    context = respond.await_args.kwargs["chat_context"]
    assert context.group_id == "20001"
    assert context.group_name is None
    assert context.sender_id == "10001"


@pytest.mark.integration
async def test_group_name_resolver_uses_onebot_and_fails_closed(
    app: App,
    after_nonebot_init: None,
) -> None:
    from agent_bot.plugin import application as chat_plugin

    async with app.test_api() as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.should_call_api(
            "get_group_info",
            {"group_id": 20001, "no_cache": False},
            result={
                "group_id": 20001,
                "group_name": "  测试\n交流群  ",
                "member_count": 10,
                "max_member_count": 200,
            },
        )
        assert await chat_plugin._resolve_group_name(bot, 20001) == "测试 交流群"

    async with app.test_api() as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.should_call_api(
            "get_group_info",
            {"group_id": 20001, "no_cache": False},
            exception=NetworkError("sensitive NapCat detail"),
        )
        assert await chat_plugin._resolve_group_name(bot, 20001) is None


@pytest.mark.integration
async def test_group_chat_resolves_conversational_mention_through_ai(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    respond = mock_response(monkeypatch, chat_plugin, "请来集合")
    counter = Mock(limit=10)
    counter.try_consume.return_value = True
    monkeypatch.setattr(chat_plugin, "message_counter", counter)
    message = Message(
        [
            MessageSegment.at(90001),
            MessageSegment.text(" 帮我 "),
            MessageSegment.at(10086),
            MessageSegment.text(" 一下，告诉他集合"),
        ]
    )
    event = group_event(message, to_me=True)
    expected = Message(
        [
            MessageSegment.at(10086),
            MessageSegment.text(" 请来集合"),
        ]
    )

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, expected, bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    assert respond.await_args.args[1] == "告诉他集合"
    counter.try_consume.assert_called_once_with(str(event.user_id))


@pytest.mark.integration
async def test_group_chat_passes_passive_native_mention_as_member_name(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify group chat passes passive native mention as member name."""

    monkeypatch.setattr(chat_plugin.plugin_config, "nickname", {"桃桃"})
    respond = mock_response(monkeypatch, chat_plugin, "我听见你们叫我了。")
    message = Message(
        [
            MessageSegment.at(10086),
            MessageSegment.text("，你去叫一下桃桃"),
        ]
    )
    event = group_event(message, nickname="a", to_me=False)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_api(
            "get_group_member_list",
            {"group_id": 20001, "no_cache": False},
            result=[
                {"user_id": 10001, "nickname": "a", "card": ""},
                {"user_id": 10086, "nickname": "乙", "card": "b"},
            ],
        )
        ctx.should_call_send(
            event,
            Message(MessageSegment.text("我听见你们叫我了。")),
            bot=bot,
        )
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    assert respond.await_args.args[1] == "b，你去叫一下桃桃"
    assert respond.await_args.kwargs["chat_context"].sender_name == "a"


@pytest.mark.integration
async def test_group_chat_resolves_unique_member_name_for_conversational_mention(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    respond = mock_response(monkeypatch, chat_plugin, "请查看群消息")
    event = group_event("请你艾特技术部小明，告诉他看消息", to_me=True)
    expected = Message(
        [
            MessageSegment.at(10086),
            MessageSegment.text(" 请查看群消息"),
        ]
    )

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_api(
            "get_group_member_list",
            {"group_id": 20001, "no_cache": False},
            result=[
                {"user_id": 10010, "nickname": "小明", "card": ""},
                {"user_id": 10086, "nickname": "李明", "card": "技术部小明"},
            ],
        )
        ctx.should_call_send(event, expected, bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    assert respond.await_args.args[1] == "告诉他看消息"


@pytest.mark.integration
async def test_group_chat_sends_unresolved_named_mention_to_ai(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify group chat sends unresolved named mention to ai."""

    respond = mock_response(monkeypatch, chat_plugin, "我先帮你确认一下具体是哪位。")
    counter = Mock(limit=10)
    counter.try_consume.return_value = True
    monkeypatch.setattr(chat_plugin, "message_counter", counter)
    prompt = "请你艾特不存在的人，告诉他集合"
    event = group_event(prompt, to_me=True)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_api(
            "get_group_member_list",
            {"group_id": 20001, "no_cache": False},
            result=[{"user_id": 10010, "nickname": "小明", "card": ""}],
        )
        ctx.should_call_send(
            event,
            Message(MessageSegment.text("我先帮你确认一下具体是哪位。")),
            bot=bot,
        )
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    assert respond.await_args.args[1] == prompt
    assert "mention_member" not in respond.await_args.kwargs["chat_tools"]
    counter.try_consume.assert_called_once_with(str(event.user_id))


@pytest.mark.integration
async def test_auto_mention_ai_path_does_not_add_a_second_reminder_tool(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    respond = mock_response(monkeypatch, chat_plugin, "请查看群消息")
    monkeypatch.setattr(
        chat_plugin._tool_session_manager,
        "available_tools",
        lambda _event: ("mention_member",),
    )
    load_members = AsyncMock(
        return_value=[
            {"user_id": "10086", "nickname": "小明", "card": "技术部小明"}
        ]
    )
    monkeypatch.setattr(chat_plugin, "_load_group_members", load_members, raising=False)
    event = group_event("请你艾特技术部小明，告诉他看消息", to_me=True)
    expected = Message(
        [
            MessageSegment.at(10086),
            MessageSegment.text(" 请查看群消息"),
        ]
    )

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, expected, bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    load_members.assert_awaited_once()
    respond.assert_awaited_once()
    assert respond.await_args.args[1] == "告诉他看消息"
    assert respond.await_args.kwargs["chat_tools"] == ()


@pytest.mark.integration
@pytest.mark.parametrize("prompt", ["聊聊今天的心情", "我走8 8，轮到dd了", "记得调用at的工具"])
async def test_group_chat_keeps_authorized_local_tools_without_explicit_request(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin, prompt: str,
) -> None:
    """普通群聊和后续对话均向 Agent 提供已授权本地工具，不提前执行工具。"""

    monkeypatch.setattr(chat_plugin.plugin_config, "chat_auto_mention_enabled", False)
    monkeypatch.setattr(
        chat_plugin._tool_session_manager,
        "available_tools",
        lambda _event: (
            "naraka_query", "mention_member", "local_workflow_generate_image",
        ),
    )
    event = group_event(prompt, to_me=True)
    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_finished(chat_plugin.natural_chat)

    enqueue = chat_plugin.conversation_queue.enqueue
    enqueue.assert_awaited_once()
    assert enqueue.await_args.args[0].chat_tools == (
        "mention_member", "local_workflow_generate_image",
    )


@pytest.mark.integration
async def test_bot_group_sync_persists_each_bounded_napcat_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent_bot.plugin import application as chat_plugin

    store = Mock()
    store.replace_group = AsyncMock()
    monkeypatch.setattr(chat_plugin, "_group_member_store", store, raising=False)
    bot = Mock(self_id="90001")
    bot.get_group_list = AsyncMock(
        return_value=[
            {"group_id": 20001, "group_name": "测试一群"},
            {"group_id": 20002, "group_name": "测试二群"},
        ]
    )
    bot.get_group_member_list = AsyncMock(
        side_effect=[
            [{"user_id": 10001, "nickname": "成员一", "card": ""}],
            [{"user_id": 10002, "nickname": "成员二", "card": ""}],
        ]
    )

    await chat_plugin._sync_bot_group_members(bot)

    assert bot.get_group_list.await_args.kwargs == {"no_cache": False}
    assert bot.get_group_member_list.await_count == 2
    assert store.replace_group.await_args_list[0].kwargs == {
        "bot_id": "90001",
        "group_id": "20001",
        "group_name": "测试一群",
        "members": [{"user_id": 10001, "nickname": "成员一", "card": ""}],
    }


@pytest.mark.integration
async def test_lifecycle_connect_triggers_group_member_sync(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    sync = AsyncMock()
    monkeypatch.setattr(chat_plugin, "_sync_bot_group_members", sync)
    event = LifecycleMetaEvent(
        time=1,
        self_id=90001,
        post_type="meta_event",
        meta_event_type="lifecycle",
        sub_type="connect",
    )

    async with app.test_matcher(chat_plugin.group_member_lifecycle) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)

    sync.assert_awaited_once_with(bot)


@pytest.mark.integration
async def test_group_member_notices_incrementally_update_persisted_directory(
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    store = Mock()
    store.upsert_member = AsyncMock()
    store.delete_member = AsyncMock()
    monkeypatch.setattr(chat_plugin, "_group_member_store", store, raising=False)
    bot = Mock(self_id="90001")
    bot.get_group_member_info = AsyncMock(
        return_value={
            "user_id": 10086,
            "nickname": "小明",
            "card": "技术部小明",
            "role": "member",
        }
    )
    increase = GroupIncreaseNoticeEvent(
        time=1,
        self_id=90001,
        post_type="notice",
        notice_type="group_increase",
        sub_type="approve",
        user_id=10086,
        group_id=20001,
        operator_id=10000,
    )
    decrease = GroupDecreaseNoticeEvent(
        time=2,
        self_id=90001,
        post_type="notice",
        notice_type="group_decrease",
        sub_type="leave",
        user_id=10086,
        group_id=20001,
        operator_id=10086,
    )

    await chat_plugin._handle_group_member_notice_impl(bot, increase)
    await chat_plugin._handle_group_member_notice_impl(bot, decrease)

    bot.get_group_member_info.assert_awaited_once_with(
        group_id=20001,
        user_id=10086,
        no_cache=False,
    )
    store.upsert_member.assert_awaited_once_with(
        bot_id="90001",
        group_id="20001",
        group_name=None,
        member={
            "user_id": 10086,
            "nickname": "小明",
            "card": "技术部小明",
            "role": "member",
        },
    )
    store.delete_member.assert_awaited_once_with("90001", "20001", "10086")


@pytest.mark.integration
async def test_group_chat_rejects_ambiguous_named_mention_without_ai(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify group chat rejects ambiguous named mention without ai."""

    respond = mock_response(monkeypatch, chat_plugin, "不应调用 AI")
    event = group_event("帮我艾特小明，集合", to_me=True)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_api(
            "get_group_member_list",
            {"group_id": 20001, "no_cache": False},
            result=[
                {"user_id": 10010, "nickname": "小明", "card": ""},
                {"user_id": 10086, "nickname": "小明", "card": ""},
            ],
        )
        ctx.should_call_send(
            event,
            Message(MessageSegment.text("模型生成的校验回复。")),
            bot=bot,
        )
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_not_awaited()


@pytest.mark.integration
async def test_invalid_conversational_mention_honors_message_limit(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify invalid conversational mention honors message limit."""

    respond = mock_response(monkeypatch, chat_plugin, "不应调用 AI")
    counter = Mock(limit=1)
    counter.try_consume.return_value = False
    monkeypatch.setattr(chat_plugin, "message_counter", counter)
    event = group_event(
        Message(
            [
                MessageSegment.text("帮我 "),
                MessageSegment.at(10086),
                MessageSegment.at(10010),
                MessageSegment.text(" 集合"),
            ]
        ),
        to_me=True,
    )

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_finished(chat_plugin.natural_chat)

    counter.try_consume.assert_called_once_with(str(event.user_id))
    respond.assert_not_awaited()


@pytest.mark.integration
async def test_disabled_auto_mention_falls_back_to_chat(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify disabled auto mention falls back to chat."""

    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_auto_mention_enabled",
        False,
    )
    respond = mock_response(monkeypatch, chat_plugin, "普通聊天回答")
    event = group_event("帮我艾特小明，集合", to_me=True)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("普通聊天回答")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()


@pytest.mark.integration
async def test_auto_mention_is_independent_from_agent_mention_tool(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_mention_tool_enabled",
        False,
    )
    respond = mock_response(monkeypatch, chat_plugin, "该睡觉了")
    event = group_event("桃桃，at一下1145000687，提醒他该睡觉了", to_me=True)
    expected = Message(
        [
            MessageSegment.at(1145000687),
            MessageSegment.text(" 该睡觉了"),
        ]
    )

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, expected, bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)

    respond.assert_awaited_once()
    assert respond.await_args.args[1] == "提醒他该睡觉了"


@pytest.mark.integration
async def test_group_keyword_reply_uses_llm_and_mentions_sender(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    reply_chat: AsyncMock,
) -> None:
    """配置内容只作为生成事实，实际发送模型正文和真实发送者 At。"""
    monkeypatch.setattr(chat_plugin.plugin_config, "nickname", {"小派"})
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_keyword_at_replies",
        {"特别关键词": "固定回复 [CQ:at,qq=all]"},
    )
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_group_recent_context_enabled",
        True,
    )
    store = RecentGroupContextStore(
        message_limit=10,
        max_chars=1000,
        ttl_seconds=60,
        max_groups=10,
    )
    monkeypatch.setattr(chat_plugin, "group_recent_context_store", store)
    auto_speak = Mock()
    auto_speak.accepts_message.return_value = True
    auto_speak.handle_group_message = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_plugin, "auto_speak_service", auto_speak)
    respond = mock_response(monkeypatch, chat_plugin, "不应调用 AI")
    event = group_event("小派，这里包含特别关键词", user_id=10086, to_me=False)
    expected = Message(
        [
            MessageSegment.at(10086),
            MessageSegment.text(" 模型生成的校验回复。"),
        ]
    )

    async with app.test_matcher(
        [chat_plugin.keyword_at_reply, chat_plugin.natural_chat]
    ) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, expected, bot=bot)
        ctx.should_finished(chat_plugin.keyword_at_reply)

    respond.assert_not_awaited()
    assert "KEYWORD_REPLY" in reply_chat.await_args.kwargs["instruction"]
    assert "固定回复 [CQ:at,qq=all]" in reply_chat.await_args.kwargs["instruction"]
    assert store.context(bot_id="90001", group_id="20001") == (
        "tester(QQ 10086)：小派，这里包含特别关键词"
    )
    auto_speak.handle_group_message.assert_not_awaited()


@pytest.mark.integration
async def test_at_command_sends_real_onebot_at_segment_with_llm_body(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
    reply_chat: AsyncMock,
) -> None:
    """真实 At 目标由代码解析，正文仅来自受限回复模型。"""
    monkeypatch.setattr(chat_plugin, "_load_group_members", AsyncMock(return_value=[
        {"user_id": 10086, "nickname": "成员", "card": ""},
    ]))
    chat_respond = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "respond", chat_respond)
    event = group_event("/at 10086 请来集合", user_id=10001)
    expected = Message(
        [
            MessageSegment.at(10086),
            MessageSegment.text(" 模型生成的校验回复。"),
        ]
    )

    async with app.test_matcher(chat_plugin.at_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, expected, bot=bot)
        ctx.should_finished(chat_plugin.at_command)

    chat_respond.assert_not_awaited()
    assert "COMMAND_RESULT" in reply_chat.await_args.kwargs["instruction"]
    assert "请来集合" in reply_chat.await_args.kwargs["untrusted_facts"]
    assert "请来集合" not in reply_chat.await_args.kwargs["instruction"]


@pytest.mark.integration
async def test_at_command_accepts_native_at_segment(
    app: App,
    chat_plugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """原生 At 输入仍按确定性目标输出真实 At 和模型正文。"""
    monkeypatch.setattr(chat_plugin, "_load_group_members", AsyncMock(return_value=[
        {"user_id": 10086, "nickname": "成员", "card": ""},
    ]))
    message = Message(
        [
            MessageSegment.text("/at "),
            MessageSegment.at(10086),
            MessageSegment.text(" 轮到你了"),
        ]
    )
    event = group_event(str(message), user_id=10001)
    event.message = message
    event.original_message = message
    expected = Message(
        [
            MessageSegment.at(10086),
            MessageSegment.text(" 模型生成的校验回复。"),
        ]
    )

    async with app.test_matcher(chat_plugin.at_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, expected, bot=bot)
        ctx.should_finished(chat_plugin.at_command)


@pytest.mark.integration
async def test_at_command_rejects_private_chat_and_invalid_target(
    app: App,
    chat_plugin,
) -> None:
    """私聊和非法目标均通过模型表达既定校验失败。"""
    private = private_event("/at 10086 你好")
    async with app.test_matcher(chat_plugin.at_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, private)
        ctx.should_call_send(
            private,
            Message(MessageSegment.text("模型生成的校验回复。")),
            bot=bot,
        )
        ctx.should_finished(chat_plugin.at_command)

    invalid = group_event("/at all 禁止全体提醒")
    async with app.test_matcher(chat_plugin.at_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, invalid)
        ctx.should_call_send(
            invalid,
            Message(MessageSegment.text("模型生成的校验回复。")),
            bot=bot,
        )
        ctx.should_finished(chat_plugin.at_command)


@pytest.mark.integration
@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize(
    ("message", "matcher_name", "superuser", "code", "fact", "target"),
    [
        ("/new", "reset_command", False, "PERMISSION_DENIED", "重置当前会话", None),
        ("/new", "reset_command", True, "COMMAND_RESULT", "当前会话已重置", None),
        ("/at", "at_command", False, "COMMAND_USAGE", "/at <QQ号或@成员> <内容>", None),
        ("/at 10010 集合", "at_command", False, "INVALID_INPUT", "群成员", None),
        ("/at 10086 集合", "at_command", False, "COMMAND_RESULT", "集合", 10086),
        ("特别关键词", "keyword_at_reply", False, "KEYWORD_REPLY", "配置内容", 10001),
    ],
)
async def test_command_keyword_reply_boundary_or_silence(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin, reply_chat: AsyncMock,
    message: str, matcher_name: str, superuser: bool, code: str, fact: str,
    target: int | None, fails: bool,
) -> None:
    """禁止固定文案越过发送边界，模型失败时也不能发送裸 At。"""
    monkeypatch.setattr(chat_plugin.plugin_config, "superusers", {"10001"} if superuser else set())
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_keyword_at_replies", {
        "特别关键词": "配置内容",
    })
    members = [{"user_id": 10086, "nickname": "成员", "card": ""}]
    monkeypatch.setattr(chat_plugin, "_load_group_members", AsyncMock(return_value=members))
    monkeypatch.setattr(chat_plugin, "_refresh_group_members", AsyncMock(return_value=members))
    reset = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_plugin.chat_service, "reset", reset)
    if fails:
        reply_chat.side_effect = ChatAiUnavailableError("sensitive upstream detail")
    event = group_event(message, user_id=10001, to_me=False)
    matcher = getattr(chat_plugin, matcher_name)
    expected = Message(MessageSegment.text("模型生成的校验回复。"))
    if target is not None:
        expected = Message([
            MessageSegment.at(target), MessageSegment.text(" 模型生成的校验回复。"),
        ])
    async with app.test_matcher(matcher) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="90001")
        ctx.receive_event(bot, event)
        if not fails:
            ctx.should_call_send(event, expected, bot=bot)
        ctx.should_finished(matcher)
    reply_chat.assert_awaited_once()
    assert code in reply_chat.await_args.kwargs["instruction"]
    fact_input = (
        reply_chat.await_args.kwargs["untrusted_facts"]
        if code == "COMMAND_RESULT" else reply_chat.await_args.kwargs["instruction"]
    )
    assert fact in fact_input
    assert reset.await_count == int(superuser)


@pytest.mark.integration
async def test_group_keyword_reply_ignores_unmatched_or_blocked_messages(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    monkeypatch.setattr(
        chat_plugin.plugin_config,
        "chat_keyword_at_replies",
        {"特别关键词": "固定回复"},
    )
    unmatched = group_event("普通消息", to_me=False)
    blocked = group_event("特别关键词", group_id=20002, to_me=False)

    async with app.test_matcher(chat_plugin.keyword_at_reply) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, unmatched)
        ctx.should_not_pass_rule(chat_plugin.keyword_at_reply)

    monkeypatch.setattr(
        chat_plugin,
        "access_policy",
        AccessPolicy(blocked_groups={"20002"}),
    )
    async with app.test_matcher(chat_plugin.keyword_at_reply) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, blocked)
        ctx.should_not_pass_rule(chat_plugin.keyword_at_reply)


@pytest.mark.integration
async def test_reset_command_uses_current_session(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin, reply_chat: AsyncMock,
) -> None:
    """会话清理先完成，再生成并发送成功事实。"""
    reset = AsyncMock(return_value=True)

    async def reply_after_reset(*_args, **kwargs):
        """在模型边界验证重置时序和事实。"""
        reset.assert_awaited_once()
        assert "COMMAND_RESULT" in kwargs["instruction"]
        assert "当前会话已重置" in kwargs["untrusted_facts"]
        return "模型生成的校验回复。"

    reply_chat.side_effect = reply_after_reset
    monkeypatch.setattr(chat_plugin.chat_service, "reset", reset)
    monkeypatch.setattr(chat_plugin.plugin_config, "superusers", {"10001"})
    event = private_event("/new")

    async with app.test_matcher(chat_plugin.reset_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("模型生成的校验回复。")), bot=bot)
        ctx.should_finished(chat_plugin.reset_command)

    reset.assert_awaited_once()
    assert reset.await_args.args[0].startswith("qq_")


@pytest.mark.integration
async def test_reset_command_denies_non_superuser_with_llm_without_resetting_session(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin, reply_chat: AsyncMock,
) -> None:
    """权限结论不由模型决定，拒绝文字只来自模型。"""
    reset = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_plugin.chat_service, "reset", reset)
    monkeypatch.setattr(chat_plugin.plugin_config, "superusers", {"99999"})
    event = private_event("/new", user_id=10001)

    async with app.test_matcher(chat_plugin.reset_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message(MessageSegment.text("模型生成的校验回复。")),
            bot=bot,
        )
        ctx.should_finished(chat_plugin.reset_command)

    reset.assert_not_awaited()
    assert "PERMISSION_DENIED" in reply_chat.await_args.kwargs["instruction"]


@pytest.mark.integration
async def test_queue_write_failure_replies_without_internal_detail(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin
) -> None:
    """Verify queue write failure replies without internal detail."""

    enqueue = AsyncMock(side_effect=ConnectionError("secret Redis details"))
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", enqueue)
    event = private_event("测试")

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            Message(MessageSegment.text("模型生成的校验回复。")),
            bot=bot,
        )
        ctx.should_finished(chat_plugin.natural_chat)

    enqueue.assert_awaited_once()


@pytest.mark.integration
async def test_unauthorized_private_message_does_not_reach_service(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin
) -> None:
    respond = mock_response(monkeypatch, chat_plugin, "不应发送")
    monkeypatch.setattr(
        chat_plugin,
        "access_policy",
        AccessPolicy(allowed_users={"another-user"}),
    )
    event = private_event("测试")

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.natural_chat)

    respond.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize("message", ["/chat", "/chat 你好", "/聊天 你好"])
async def test_removed_chat_command_does_not_reach_natural_chat(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin, message: str
) -> None:
    respond = mock_response(monkeypatch, chat_plugin, "不应发送")
    event = private_event(message)

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.natural_chat)

    respond.assert_not_awaited()


@pytest.mark.integration
async def test_long_reply_is_sent_as_ordered_text_chunks(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin
) -> None:
    """Verify long reply is sent as ordered text chunks."""

    mock_response(monkeypatch, chat_plugin, "甲" * 1201)
    event = private_event("长回复")

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message(MessageSegment.text("甲" * 1200)), bot=bot)
        ctx.should_call_send(event, Message(MessageSegment.text("甲")), bot=bot)
        ctx.should_finished(chat_plugin.natural_chat)


@pytest.mark.integration
async def test_consecutive_natural_chat_requests_are_both_enqueued(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin
) -> None:
    coordinator = ChatRequestCoordinator(
        cooldown_seconds=2,
        max_concurrent_requests=2,
        max_rate_limit_entries=10,
        clock=lambda: 100.0,
    )
    monkeypatch.setattr(chat_plugin.chat_service, "_request_coordinator", coordinator)
    enqueue = AsyncMock(return_value="1-0")
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", enqueue)

    adapter = nonebot.get_adapter(Adapter)
    for message in ("第一条", "第二条"):
        event = private_event(message, user_id=10501)
        async with app.test_matcher(chat_plugin.natural_chat) as ctx:
            bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
            ctx.receive_event(bot, event)
            ctx.should_finished(chat_plugin.natural_chat)

    assert [call.args[0].prompt for call in enqueue.await_args_list] == [
        "第一条",
        "第二条",
    ]


@pytest.mark.integration
async def test_queue_full_finishes_silently_without_reply_model(
    app: App,
    monkeypatch: pytest.MonkeyPatch,
    chat_plugin,
) -> None:
    """Verify queue full finishes silently without reply model."""

    enqueue = AsyncMock(
        side_effect=ConversationQueueFullError("sensitive queue detail")
    )
    monkeypatch.setattr(chat_plugin.conversation_queue, "enqueue", enqueue)
    event = private_event("测试入队失败")

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_finished(chat_plugin.natural_chat)

    enqueue.assert_awaited_once()


@pytest.mark.integration
async def test_in_flight_conversation_is_persisted_instead_of_rejected(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin
) -> None:
    respond = AsyncMock(side_effect=ChatBusyError("request already in progress"))
    monkeypatch.setattr(chat_plugin.chat_service, "respond", respond)
    event = private_event("第二条")

    async with app.test_matcher(chat_plugin.natural_chat) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_finished(chat_plugin.natural_chat)

    chat_plugin.conversation_queue.enqueue.assert_awaited_once()
    respond.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize(("command", "argument", "method", "result", "fragments"), [
    ("永劫", "17605500010163", "query",
     NarakaResult.model_validate({"player_info": {"name": "玩家"}, "server_desc": "网易国服"}),
     ("永劫无间战绩", "玩家", "网易国服")),
    ("永劫", "白小痴", "query_by_nickname",
     NarakaResult.model_validate({"player_info": {"name": "白小痴"}}), ("白小痴",)),
    ("永劫", "ukg1000012345678900001.1700000000", "query_match_detail",
     NarakaMatchDetail.model_validate({
         "name": "玩家", "rank": 1, "data": [{"desc": "总伤害", "value": 16742}],
     }),
     ("玩家", "16742", "1")),
    ("永劫详情", "ukg1000012345678900001.1700000000", "query_match_detail",
     NarakaMatchDetail.model_validate({"name": "玩家", "rank": 1}), ("玩家", "1")),
    ("永劫搜索", "测试 昵称", "search_players",
     NarakaPlayerSearchResult.model_validate({
         "query": "测试 昵称", "players": [
             {"name": "第一名", "game_id": "7pra000013743400100163", "server": "网易国服"},
             {"name": "第二名", "game_id": "wxyz000000987654321163", "server": "163"},
         ],
     }), ("7pra000013743400100163", "wxyz000000987654321163", "2")),
])
async def test_naraka_results_use_single_composer_with_exact_facts(
    app: App, monkeypatch, chat_plugin, reply_chat, command, argument, method, result, fragments,
) -> None:
    """角色、比赛、昵称和搜索结果只生成一次且完整传入确定事实。"""

    query = AsyncMock(return_value=result)
    monkeypatch.setattr(chat_plugin, "naraka_service", type("Service", (), {method: query})())
    legacy_summary = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "ask_once", legacy_summary)
    compose = AsyncMock(wraps=chat_plugin._reply_service.compose)
    monkeypatch.setattr(chat_plugin._reply_service, "compose", compose)
    event = private_event(f"/{command} {argument}")
    matcher = (
        chat_plugin.naraka_search_command if command == "永劫搜索" else chat_plugin.naraka_command
    )
    async with app.test_matcher([matcher, chat_plugin.natural_chat]) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message("模型生成的校验回复。"), bot=bot)
        ctx.should_finished(matcher)
    query.assert_awaited_once()
    assert query.await_args.args[1] == argument
    legacy_summary.assert_not_awaited()
    reply_chat.assert_awaited_once()
    compose.assert_awaited_once()
    intent = compose.await_args.args[0]
    assert intent.code == "COMMAND_RESULT"
    assert intent.facts.preserve_facts is True
    assert intent.user == str(event.user_id)
    assert intent.original_prompt == event.get_plaintext()
    for fragment in fragments:
        assert fragment in intent.facts.content
        assert fragment in reply_chat.await_args.kwargs["untrusted_facts"]
    assert "必须准确保留" in reply_chat.await_args.kwargs["instruction"]
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()




@pytest.mark.integration
async def test_group_to_me_naraka_search_uses_dedicated_composer_route(
    app: App, monkeypatch, chat_plugin, reply_chat,
) -> None:
    """被 At 的群搜索只触发专用查询与 Composer，不落入战绩或自然聊天。"""

    service = Mock(
        search_players=AsyncMock(return_value=NarakaPlayerSearchResult.model_validate({
            "query": "测试 昵称",
            "players": [{"name": "候选", "game_id": "7pra000013743400100163"}],
        })),
        query=AsyncMock(), query_by_nickname=AsyncMock(), query_match_detail=AsyncMock(),
    )
    monkeypatch.setattr(chat_plugin, "naraka_service", service)
    event = group_event("/永劫搜索 测试 昵称", to_me=True)
    async with app.test_matcher([
        chat_plugin.naraka_command, chat_plugin.naraka_search_command, chat_plugin.natural_chat,
    ]) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message("模型生成的校验回复。"), bot=bot)
        ctx.should_finished(chat_plugin.naraka_search_command)
    service.search_players.assert_awaited_once()
    assert service.search_players.await_args.args[1] == "测试 昵称"
    service.query.assert_not_awaited()
    service.query_by_nickname.assert_not_awaited()
    service.query_match_detail.assert_not_awaited()
    reply_chat.assert_awaited_once()
    assert "7pra000013743400100163" in reply_chat.await_args.kwargs["untrusted_facts"]
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()


@pytest.mark.integration
async def test_naraka_alphanumeric_game_id_routes_to_query_and_composer(
    app: App, monkeypatch, chat_plugin, reply_chat,
) -> None:
    """字母数字 game_id 必须按角色 ID 查询而非昵称搜索并仅输出 Composer 原文。"""

    game_id = "7pra000013743400100163"
    result = NarakaResult.model_validate({"player_info": {"name": "玩家"}})
    service = Mock(
        query=AsyncMock(return_value=result),
        query_by_nickname=AsyncMock(), query_match_detail=AsyncMock(), search_players=AsyncMock(),
    )
    monkeypatch.setattr(chat_plugin, "naraka_service", service)
    event = private_event(f"/永劫 {game_id}")
    async with app.test_matcher([
        chat_plugin.naraka_command, chat_plugin.naraka_search_command, chat_plugin.natural_chat,
    ]) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, Message("模型生成的校验回复。"), bot=bot)
        ctx.should_finished(chat_plugin.naraka_command)
    service.query.assert_awaited_once()
    assert service.query.await_args.args[1] == game_id
    service.query_by_nickname.assert_not_awaited()
    service.query_match_detail.assert_not_awaited()
    service.search_players.assert_not_awaited()
    reply_chat.assert_awaited_once()
    assert "玩家" in reply_chat.await_args.kwargs["untrusted_facts"]
    chat_plugin.conversation_queue.enqueue.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("failure", [ChatAiUnavailableError("private detail"), None])
async def test_naraka_composer_failure_or_blank_is_silent(
    monkeypatch, chat_plugin, reply_chat, group, failure,
) -> None:
    """Composer 失败或空白时不能直发战绩或静态 AI 点评兜底。"""

    monkeypatch.setattr(chat_plugin, "naraka_service", Mock(
        query=AsyncMock(return_value=NarakaResult.model_validate({
            "player_info": {"name": "玩家"},
        })),
    ))
    reply_chat.side_effect = failure
    reply_chat.return_value = "  "
    event = (group_event if group else private_event)("/永劫 17605500010163")
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await chat_plugin._naraka_handler.handle_query(
            Mock(self_id="90001"), event, Message("17605500010163"), matcher=matcher,
        )
    matcher.send.assert_not_awaited()
    matcher.finish.assert_awaited_once_with()
    reply_chat.assert_awaited_once()


@pytest.mark.integration
@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize("error", [
    NarakaAuthenticationError("private detail"), NarakaUnavailableError("private detail"),
    NarakaDataUpdatingError("private detail"), ValueError("private detail"),
])
async def test_naraka_query_failure_is_silent(monkeypatch, chat_plugin, reply_chat, search, error):
    """供应商与查询校验失败不得发送硬编码文本或触发额外模型请求。"""

    query = AsyncMock(side_effect=error)
    monkeypatch.setattr(chat_plugin, "naraka_service", Mock(query=query, search_players=query))
    handler = chat_plugin._naraka_handler
    handle = handler.handle_search if search else handler.handle_query
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await handle(
            Mock(self_id="90001"), private_event("/永劫 17605500010163"),
            Message("17605500010163"), matcher=matcher,
        )
    matcher.send.assert_not_awaited()
    matcher.finish.assert_awaited_once_with()
    reply_chat.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.parametrize(("command", "argument", "kind", "fact"), [
    ("永劫", "", "COMMAND_USAGE", "/永劫"),
    ("永劫", "1234567890123456789", "COMMAND_USAGE", "/永劫"),
    ("永劫", "ukg1000017605500010163.bad-time", "COMMAND_USAGE", "/永劫"),
    ("永劫搜索", "", "COMMAND_USAGE", "/永劫搜索"),
    ("永劫", "17605500010163", "FEATURE_UNAVAILABLE", "永劫查询"),
    ("永劫搜索", "昵称", "FEATURE_UNAVAILABLE", "永劫查询"),
])
async def test_naraka_validation_uses_composer(
    monkeypatch, chat_plugin, reply_chat, command, argument, kind, fact,
) -> None:
    """用法与未配置功能通过共享回复服务生成解释。"""

    monkeypatch.setattr(chat_plugin, "naraka_service", None)
    monkeypatch.setattr(chat_plugin, "_naraka_default_role_id", None)
    handler = chat_plugin._naraka_handler
    handle = handler.handle_search if command == "永劫搜索" else handler.handle_query
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    with pytest.raises(FinishedException):
        await handle(
            Mock(self_id="90001"), private_event(f"/{command} {argument}"),
            Message(argument), matcher=matcher,
        )
    matcher.send.assert_awaited_once_with(Message("模型生成的校验回复。"))
    assert kind in reply_chat.await_args.kwargs["instruction"]
    assert fact in reply_chat.await_args.kwargs["instruction"]


@pytest.mark.integration
@pytest.mark.parametrize("message", [
    "/永劫X 17605500010163", "/永劫搜索X 白小痴",
    "/永劫详情X ukg1000017605500010163.1786282570",
])
async def test_naraka_commands_require_complete_command_keyword(app: App, chat_plugin, message):
    """不完整命令名不能触发查询副作用。"""

    event = private_event(message)
    async with app.test_matcher(
        [chat_plugin.naraka_command, chat_plugin.naraka_search_command],
    ) as ctx:
        bot = ctx.create_bot(base=Bot, adapter=nonebot.get_adapter(Adapter), self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.naraka_command)
        ctx.should_not_pass_rule(chat_plugin.naraka_search_command)


@pytest.mark.integration
async def test_unauthorized_naraka_command_does_not_query(
    app: App, monkeypatch: pytest.MonkeyPatch, chat_plugin
) -> None:
    query = AsyncMock()
    service = type("NarakaService", (), {"query": query})()
    monkeypatch.setattr(chat_plugin, "naraka_service", service)
    monkeypatch.setattr(
        chat_plugin,
        "access_policy",
        AccessPolicy(allowed_users={"another-user"}),
    )
    event = private_event("/永劫 17605500010163")

    async with app.test_matcher(chat_plugin.naraka_command) as ctx:
        adapter = nonebot.get_adapter(Adapter)
        bot = ctx.create_bot(base=Bot, adapter=adapter, self_id="90001")
        ctx.receive_event(bot, event)
        ctx.should_not_pass_rule(chat_plugin.naraka_command)

    query.assert_not_awaited()
