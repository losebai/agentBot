"""直接画图命令的来源、结构化消息与静默失败测试。"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.exception import NetworkError
from nonebot.exception import FinishedException

from agent_bot.ai.chat_errors import ChatAiUnavailableError
from agent_bot.model_guard import ModelNotAvailableError
from agent_bot.services.chat import ChatBusyError, ChatRateLimitedError, PromptTooLongError
from agent_bot.services.conversation_queue import ConversationQueueFullError
from agent_bot.services.workflow_execution import GeneratedWorkflowImage


@pytest.fixture
def drawing(monkeypatch: pytest.MonkeyPatch, chat_plugin) -> SimpleNamespace:
    """保留真实命令、Composer 和网关，仅隔离模型及外部工作流。"""

    monkeypatch.setattr(chat_plugin.plugin_config, "chat_local_workflow_enabled", True)
    monkeypatch.setattr(
        chat_plugin.plugin_config, "chat_local_workflow_default_key", "portrait",
    )
    workflow = AsyncMock(return_value=GeneratedWorkflowImage(media_type="image/png", data=b"image"))
    monkeypatch.setattr(chat_plugin, "workflow_service", SimpleNamespace(generate=workflow))
    accept = Mock(side_effect=lambda _session, prompt: prompt.strip())
    start = AsyncMock(return_value="模型生成的开画答复。")
    composer = AsyncMock(return_value="模型生成的校验回复。")
    commit = AsyncMock()
    enqueue = AsyncMock(return_value="1-0")
    monkeypatch.setattr(chat_plugin.image_queue, "enqueue", enqueue)
    monkeypatch.setattr(chat_plugin.chat_service, "accept_immediate", accept)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_image_start_queued", start)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", composer)
    monkeypatch.setattr(chat_plugin.chat_service, "commit_queued", commit)
    counter = Mock(return_value=True)
    monkeypatch.setattr(chat_plugin.message_counter, "try_consume", counter)
    matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    return SimpleNamespace(
        plugin=chat_plugin, workflow=workflow, accept=accept, start=start,
        composer=composer, commit=commit, matcher=matcher, counter=counter, enqueue=enqueue,
    )


def draw_event(prompt: str = "猫", *, group: bool = False):
    """构造带真实发送者身份的 OneBot 事件。"""

    event_type = GroupMessageEvent if group else PrivateMessageEvent
    payload = {
        "time": 1, "self_id": 90001, "post_type": "message", "message_id": 1,
        "user_id": 10001, "message": Message(f"/画图 {prompt}"),
        "raw_message": f"/画图 {prompt}", "font": 0,
        "sender": {"user_id": 10001, "nickname": "tester"},
        "message_type": "group" if group else "private",
        "sub_type": "normal" if group else "friend",
    }
    if group:
        payload["group_id"] = 20001
    return event_type.model_validate(payload)


async def run_draw(drawing, prompt: str = "猫", *, group: bool = False) -> None:
    """执行真实画图处理器并验证无固定附加文字地结束。"""

    with pytest.raises(FinishedException):
        await drawing.plugin._drawing_handler.handle(
            Mock(self_id="90001"), draw_event(prompt, group=group), Message(prompt),
            matcher=drawing.matcher,
        )
    drawing.matcher.finish.assert_awaited_once_with()


@pytest.mark.parametrize("group", [False, True])
async def test_draw_replies_then_enqueues_without_waiting_for_image(drawing, group: bool) -> None:
    """开画答复送达后只入队，不等待外部图片生成或发送。"""

    order = []
    drawing.enqueue.side_effect = lambda _: order.append("enqueue") or "1-0"
    drawing.matcher.send.side_effect = lambda message: order.append(
        "image" if Message(message)[0].type == "image" else "text"
    )
    drawing.start.side_effect = lambda *_, **__: (
        order.append("llm") or "模型生成的开画答复。"
    )
    await run_draw(drawing, group=group)
    messages = [Message(call.args[0]) for call in drawing.matcher.send.await_args_list]
    assert len(messages) == 1
    assert [segment.type for segment in messages[0]] == (["at", "text"] if group else ["text"])
    if group:
        assert messages[0][0].data["qq"] == "10001"
    assert messages[0].extract_plain_text().strip() == "模型生成的开画答复。"
    assert order == ["llm", "text", "enqueue"]
    assert drawing.commit.await_args.args[2] == "模型生成的开画答复。\n[绘图任务已提交]"
    drawing.composer.assert_not_awaited()
    drawing.workflow.assert_not_awaited()
    task = drawing.enqueue.await_args.args[0]
    assert task.background_image
    assert task.workflow_request.workflow_key == "portrait"
    assert task.workflow_request.positive_prompt == "猫"
    assert task.workflow_request.negative_prompt == ""


@pytest.mark.parametrize("failure", [
    ChatAiUnavailableError("private detail"), ModelNotAvailableError("private detail"),
    ChatBusyError(), ChatRateLimitedError(1), ConversationQueueFullError(), None,
])
async def test_draw_failed_or_blank_start_does_not_submit(drawing, failure) -> None:
    """开画答复失败或为空时不得先行提交绘图任务。"""

    drawing.start.side_effect = failure
    drawing.start.return_value = "   "
    await run_draw(drawing, group=True)
    drawing.matcher.send.assert_not_awaited()
    drawing.workflow.assert_not_awaited()
    drawing.commit.assert_not_awaited()
    drawing.composer.assert_not_awaited()


@pytest.mark.parametrize(("scenario", "kind", "fact"), [
    ("usage", "COMMAND_USAGE", "/画图 <提示词>"),
    ("disabled", "FEATURE_UNAVAILABLE", "本地绘图工作流"),
    ("long", "INVALID_INPUT", "画图提示词"),
    ("enqueue_failed", "TASK_FAILED", "提交绘图任务"),
])
async def test_draw_validation_uses_composer(drawing, monkeypatch, scenario, kind, fact):
    """业务失败文字必须由 Composer 生成且不能泄露原始异常。"""

    if scenario == "disabled":
        monkeypatch.setattr(
            drawing.plugin.plugin_config, "chat_local_workflow_enabled", False,
        )
    elif scenario == "long":
        drawing.accept.side_effect = PromptTooLongError()
    elif scenario == "enqueue_failed":
        drawing.enqueue.side_effect = ConnectionError("private detail")
    await run_draw(drawing, "" if scenario == "usage" else "猫")
    assert drawing.matcher.send.await_args.args[0] == Message("模型生成的校验回复。")
    instruction = drawing.composer.await_args.kwargs["instruction"]
    assert kind in instruction
    assert fact in instruction
    assert "private" not in instruction
    assert drawing.start.await_count == (1 if scenario == "enqueue_failed" else 0)


@pytest.mark.parametrize("failure", [
    ChatBusyError(), ChatRateLimitedError(1), ConversationQueueFullError(),
])
async def test_draw_admission_overload_is_silent(drawing, failure) -> None:
    """繁忙、限流与队列满不启动工作流也不生成解释文字。"""

    drawing.accept.side_effect = failure
    await run_draw(drawing)
    drawing.matcher.send.assert_not_awaited()
    drawing.workflow.assert_not_awaited()
    drawing.composer.assert_not_awaited()


async def test_draw_message_limit_is_silent(drawing) -> None:
    """消息额度耗尽不得调用模型解释限流。"""

    drawing.counter.return_value = False
    await run_draw(drawing)
    drawing.matcher.send.assert_not_awaited()
    drawing.workflow.assert_not_awaited()
    drawing.composer.assert_not_awaited()


async def test_draw_composer_failure_is_silent(drawing) -> None:
    """校验回复生成失败时不得发送固定兜底。"""

    drawing.composer.side_effect = ChatAiUnavailableError("private detail")
    await run_draw(drawing, "")
    drawing.matcher.send.assert_not_awaited()


async def test_draw_start_send_failure_does_not_submit(drawing) -> None:
    """开画文字发送失败时不能提交任务或写入成功历史。"""

    drawing.matcher.send.side_effect = NetworkError("text send failed")
    with pytest.raises(NetworkError, match="text send failed"):
        await drawing.plugin._drawing_handler.handle(
            Mock(self_id="90001"), draw_event(), Message("猫"), matcher=drawing.matcher,
        )
    drawing.workflow.assert_not_awaited()
    drawing.commit.assert_not_awaited()


@pytest.mark.parametrize("stage", ["enqueue", "delivery", "commit"])
async def test_draw_guards_only_start_delivery_enqueue_and_parent_history(
    drawing, monkeypatch, stage,
) -> None:
    """只在短暂的提交阶段保护聊天历史；后台绘图不持有这把锁。"""

    entered = asyncio.Event()
    release = asyncio.Event()
    reset = AsyncMock(return_value=True)
    monkeypatch.setattr(drawing.plugin.chat_service, "reset", reset)
    reset_matcher = Mock(send=AsyncMock(), finish=AsyncMock(side_effect=FinishedException))
    bot = Mock(self_id="90001")
    event = draw_event()

    async def pause(*_args, **_kwargs):
        """将真实命令暂停在有副作用的工作流、投递或历史提交边界。"""

        entered.set()
        await release.wait()
        if stage == "enqueue":
            return "1-0"
        return None

    if stage == "enqueue":
        drawing.enqueue.side_effect = pause
    elif stage == "delivery":
        drawing.matcher.send.side_effect = pause
    else:
        drawing.commit.side_effect = pause
    drawing_task = asyncio.create_task(run_draw(drawing))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        with pytest.raises(FinishedException):
            await asyncio.wait_for(
                drawing.plugin._conversation_handler.reset(bot, event, matcher=reset_matcher),
                0.2,
            )
        reset.assert_not_awaited()
        reset_matcher.send.assert_not_awaited()
    finally:
        release.set()
        await drawing_task
    with pytest.raises(FinishedException):
        await drawing.plugin._conversation_handler.reset(bot, event, matcher=reset_matcher)
    reset.assert_awaited_once()


async def test_draw_busy_session_finishes_without_workflow_or_waiting(drawing) -> None:
    """命令已经持有运行时租约时不能等待会话锁，忙碌时沿用静默结束。"""

    session_id = drawing.plugin._event_context.session_id(Mock(self_id="90001"), draw_event())
    async with drawing.plugin.conversation_queue.session_guard(session_id):
        await asyncio.wait_for(run_draw(drawing), 0.2)
    drawing.workflow.assert_not_awaited()
    drawing.commit.assert_not_awaited()
    drawing.matcher.send.assert_not_awaited()


async def test_cancelled_direct_draw_releases_session_guard(drawing) -> None:
    """中断绘图后必须释放会话锁，不能阻止后续重置或队列请求。"""

    entered = asyncio.Event()

    async def pause(*_args, **_kwargs):
        """模拟等待外部工作流期间取消。"""

        entered.set()
        await asyncio.Event().wait()

    drawing.enqueue.side_effect = pause
    task = asyncio.create_task(run_draw(drawing))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    session_id = drawing.plugin._event_context.session_id(Mock(self_id="90001"), draw_event())
    async with drawing.plugin.conversation_queue.session_guard(session_id, wait=False):
        pass
    drawing.commit.assert_not_awaited()


async def test_drawing_start_allows_concurrent_chat_activity(drawing) -> None:
    """开画预告仍阻止重置，但不能独占整个群的正常聊天。"""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def pause(*_args, **_kwargs):
        """保持开画流程活跃，检查同群另一条请求的非等待准入。"""
        entered.set()
        await release.wait()
        return "1-0"

    drawing.enqueue.side_effect = pause
    task = asyncio.create_task(run_draw(drawing))
    await entered.wait()
    try:
        session_id = drawing.plugin._event_context.session_id(Mock(self_id="90001"), draw_event())
        async with drawing.plugin.conversation_queue.session_activity(session_id):
            pass
    finally:
        release.set()
        await task
