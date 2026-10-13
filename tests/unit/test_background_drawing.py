"""绘图提交与后台执行隔离的离线回归。"""

import asyncio
import base64
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import fakeredis.aioredis
import pytest
from pydantic import ValidationError
from test_conversation_processor import onebot_mock, queued_job
from test_workflow_inputs import i2i_definition, uploaded_image
from workflow_fakes import TextWorkflowServiceStub

from agent_bot.ai.chat_errors import ChatAiUnavailableError
from agent_bot.integrations.onebot.tooling import ChatToolSessionLease
from agent_bot.integrations.redis_queue import RedisConversationQueueStore
from agent_bot.integrations.tool_operations import RedisToolOperationStore
from agent_bot.integrations.workflow_store import WorkflowNotFoundError
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import ChatMediaItem, ChatMediaResponse
from agent_bot.services.chat import ChatService
from agent_bot.services.chat_tools import (
    ChatToolContext,
    ChatToolError,
    build_chat_tool_registry,
)
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationQueue,
    ConversationQueueFullError,
    ConversationResult,
    QueuePolicy,
    WorkflowImageArtifact,
)
from agent_bot.services.image_task_context import ImageTaskCancelled, active_image_task
from agent_bot.services.tool_operations import ImageEffect, ImageTaskEffect, ReplyEffect
from agent_bot.services.workflow_execution import (
    GeneratedWorkflowImage,
    WorkflowExecutionService,
    WorkflowImageCollector,
    WorkflowImageRequest,
)
from agent_bot.services.workflow_inputs import workflow_input_scope
from agent_bot.state import ConversationStore


@pytest.mark.parametrize("failure", ["notice", "enqueue"])
async def test_i2i_upload_then_failed_notice_or_enqueue_never_generates_or_reuploads(
    monkeypatch, chat_plugin, image_stores, failure,
):
    """真实工具、持久占位和开画发送失败链路，重试不能重复未知副作用。"""
    parent = queued_job(jobId="i2i-failed-" + failure,
                        chatTools=("local_workflow_generate_image",),
                        imageUrls=("https://gchat.qpic.cn/source",))
    scope = workflow_input_scope(parent.job_id)
    client = SimpleNamespace(
        upload_input_image=AsyncMock(return_value=uploaded_image(
            subfolder="chat-i2i/" + scope,
        )), generate=AsyncMock(),
    )
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: i2i_definition()), client=client,
    )
    bot = onebot_mock()
    if failure == "notice":
        bot.call_api.side_effect = ConnectionError("unknown delivery status")
    enqueue = AsyncMock(side_effect=ConnectionError("unknown enqueue status"))
    monkeypatch.setattr(chat_plugin, "image_queue", SimpleNamespace(enqueue=enqueue), raising=False)
    monkeypatch.setattr(chat_plugin, "_redis_client", image_stores[0]._redis)
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    processor = chat_plugin._ConversationProcessor()
    operations = RedisToolOperationStore(image_stores[0]._redis,
                                        key_prefix="i2i-fail", ttl_seconds=60)
    for _ in range(2):
        collector = WorkflowImageCollector()
        collector.submit = lambda request, text: processor._submit_image(
            parent, request, start_reply=text,
        )
        context = ChatToolContext(
            session_id=parent.session_id, user_id=parent.user_id, job_id=parent.job_id,
            allowed_tools=frozenset(parent.chat_tools), image_urls=parent.image_urls,
            workflow_request_scope=scope, workflow_collector=collector,
            workflow_service=service, operation_store=operations,
        )
        with pytest.raises(ChatToolError):
            await build_chat_tool_registry().call("local_workflow_generate_image", {
                "workflow_key": "qwen-i2i", "positive_prompt": "改蓝色",
                "start_reply": "我来修改。",
            }, context)
    client.upload_input_image.assert_awaited_once()
    client.generate.assert_not_called()
    assert bot.call_api.await_count == 1
    assert enqueue.await_count == (1 if failure == "enqueue" else 0)


@pytest.mark.parametrize("fixed", [False, True])
async def test_i2i_background_preserves_typed_reference_without_urls(
    monkeypatch, chat_plugin, image_stores, fixed,
):
    """真实提交、序列化与生成边界保持原图归属，不能拿其他任务的图。"""
    parent = queued_job(jobId="i2i-parent", imageUrls=("https://gchat.qpic.cn/source",))
    uploaded = None if fixed else uploaded_image(
        subfolder="chat-i2i/" + workflow_input_scope(parent.job_id),
    )
    request = WorkflowImageRequest(
        workflow_key="qwen-i2i", positive_prompt="改蓝色", input_image=uploaded,
        workflow_sha256="c" * 64, fixed_reference=fixed,
    )
    enqueue = AsyncMock(return_value="1-0")
    monkeypatch.setattr(chat_plugin, "image_queue", SimpleNamespace(enqueue=enqueue), raising=False)
    monkeypatch.setattr(chat_plugin, "_redis_client", image_stores[0]._redis)
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: onebot_mock())
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_local_workflow_enabled", True)
    workflow = SimpleNamespace(generate=AsyncMock(return_value=GeneratedWorkflowImage(
        media_type="image/png", data=b"\x89PNG\r\n\x1a\nimage",
    )))
    monkeypatch.setattr(chat_plugin, "workflow_service", workflow)
    processor = chat_plugin._ConversationProcessor()
    await processor._submit_image(parent, request, start_reply="我来修改这张图。")
    task = enqueue.await_args.args[0]
    serialized = task.model_dump_json(by_alias=True)
    restored = ConversationJob.model_validate_json(serialized)
    assert restored.workflow_request.input_image == uploaded
    assert restored.image_urls == restored.video_urls == ()
    assert "https://" not in serialized and "base64" not in serialized
    assert len(serialized.encode()) < 128 * 1024
    await processor.generate(restored)
    assert workflow.generate.await_args.kwargs["input_image"] == uploaded
    assert workflow.generate.await_args.kwargs["workflow_sha256"] == "c" * 64
    assert restored.workflow_request.fixed_reference is fixed
    if not fixed:
        with pytest.raises(ValidationError, match="scope"):
            ConversationJob.model_validate({
                **restored.model_dump(by_alias=True), "jobId": "image-other",
            })


@pytest.fixture
async def image_stores():
    """独立的文本及绘图 Redis Stream，避免全局插件状态掩盖隔离问题。"""

    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    stores = tuple(RedisConversationQueueStore(
        redis, key_prefix=f"test:background:{name}", group_name="workers",
        consumer_name="initial", max_pending=64, result_ttl_seconds=60,
    ) for name in ("text", "image"))
    for store in stores:
        await store.initialize()
        # 该夹具不恢复历史任务，模拟启动器已经完成空队列的恢复扫描。
        await store.reclaim(count=1, min_idle_ms=0)
    try:
        yield stores
    finally:
        await redis.aclose()


def background_job(*, cloud: bool = False, job_id: str = "background-job"):
    """用合法类型构造只携带一种媒体来源的后台任务。"""

    return queued_job(
        jobId=job_id, chatTools=(), backgroundImage=True,
        mediaOutput="IMAGE" if cloud else None,
        workflowRequest=None if cloud else WorkflowImageRequest(
            workflow_key="portrait", positive_prompt="a cat",
        ),
    )


@pytest.mark.parametrize("part", [0, 1, "failure"])
async def test_cancel_before_media_or_during_completion_never_sends(
    monkeypatch, chat_plugin, image_stores, part,
) -> None:
    """完成文案生成中收到取消，必须在真正发送 QQ 前再次检查。"""
    cancelled = part == 0

    async def check_cancelled() -> None:
        """模拟持久化取消标记的读取。"""
        if cancelled:
            raise ImageTaskCancelled()

    async def completion(*_args, **_kwargs) -> str:
        """模拟 LLM 等待期间用户请求取消。"""
        nonlocal cancelled
        cancelled = True
        return "图片完成啦。"

    monkeypatch.setattr(chat_plugin, "_redis_client", image_stores[1]._redis)
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", completion)
    result = ConversationResult(kind="WORKFLOW_IMAGES", workflowImages=(WorkflowImageArtifact(
        mimeType="image/png", data=base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode(),
    ),))
    token = active_image_task.set(SimpleNamespace(check_cancelled=check_cancelled))
    try:
        with pytest.raises(ImageTaskCancelled):
            processor = chat_plugin._ConversationProcessor()
            if part == "failure":
                await processor.deliver_failure(background_job(), RuntimeError("workflow failed"))
            else:
                await processor.deliver_part(background_job(), result, part)
        bot.call_api.assert_not_awaited()
    finally:
        active_image_task.reset(token)


@pytest.mark.parametrize("user_image", [False, True])
async def test_legacy_completed_image_operation_restores_after_upgrade(
    monkeypatch, chat_plugin, image_stores, user_image,
):
    """旧版已保存图片但未写队列结果时，新版必须恢复原操作而非冲突或重画。"""
    job = background_job()
    if user_image:
        request = WorkflowImageRequest(
            workflow_key="qwen-i2i", positive_prompt="cat", input_image=uploaded_image(),
            workflow_sha256="c" * 64,
        )
        # 直接测试生成阶段，用户图片作用域校验由已有提交/恢复测试覆盖。
        job = job.model_copy(update={"workflow_request": request})
    request = job.workflow_request
    legacy_json = request.model_dump_json(exclude_none=True, exclude={"fixed_reference"})
    signature = hashlib.sha256(legacy_json.encode()).hexdigest()
    store = RedisToolOperationStore(
        image_stores[0]._redis, key_prefix="legacy-fixed", ttl_seconds=60,
    )
    await store.begin(job.job_id, "local_workflow_generate_image", signature)
    saved = ImageEffect(workflow_key=request.workflow_key, media_type="image/png",
                        data=base64.b64encode(b"\x89PNG\r\n\x1a\nsaved-image").decode())
    await store.complete(job.job_id, "local_workflow_generate_image", signature, saved)
    processor = chat_plugin._ConversationProcessor()
    monkeypatch.setattr(processor, "_operation_store_provider", lambda: store)
    client = SimpleNamespace(generate=AsyncMock(side_effect=AssertionError("must not regenerate")))
    monkeypatch.setattr(processor, "_workflow_service_provider", lambda: client)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_local_workflow_enabled", True)
    artifact = await processor._generate_workflow(job)
    assert artifact.workflow_images[0].data == saved.data
    client.generate.assert_not_called()


@pytest.mark.parametrize("cloud", [False, True])
async def test_background_image_allows_same_session_chat_and_reset(
    monkeypatch, chat_plugin, image_stores, cloud,
) -> None:
    """真实两队列及 ChatService 下，出图中原会话能回复、重置且不被旧历史覆盖。"""

    entered, release = asyncio.Event(), asyncio.Event()
    png = b"\x89PNG\r\n\x1a\nimage"

    async def generate(**_kwargs):
        """挂起绘图，普通回复和重置必须在此释放前完成。"""

        entered.set()
        await release.wait()
        return GeneratedWorkflowImage(media_type="image/png", data=png)

    async def generate_media(**kwargs):
        """云端使用真实 ChatService 的媒体并发与会话互斥。"""

        await generate(**kwargs)
        return ChatMediaResponse(items=(ChatMediaItem(
            type="IMAGE", mimeType="image/png", data=base64.b64encode(png).decode(),
        ),))

    history = ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60)
    service = ChatService(
        client=SimpleNamespace(complete=AsyncMock(return_value="可以继续聊天"),
                               generate_media=generate_media),
        store=history, system_prompt="正常聊天", max_input_chars=100, cooldown_seconds=0,
        max_concurrent_requests=1, max_concurrent_media_requests=1,
        model_router=ContentModelRouter("text", "vision", "image", "audio"),
    )
    workflow = SimpleNamespace(generate=AsyncMock(side_effect=generate))
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "chat_service", service)
    monkeypatch.setattr(chat_plugin, "workflow_service", workflow)
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    monkeypatch.setattr(chat_plugin, "_redis_client", image_stores[0]._redis)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_local_workflow_enabled", True)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_image_generation_enabled", True)
    processor = chat_plugin._ConversationProcessor()
    policy = QueuePolicy(1, 2, .01, .01, 600, media_workers=1)
    queues = tuple(ConversationQueue(
        store=store, processor=processor, policy=policy, clock=lambda: 1_800_000_000,
    ) for store in image_stores)
    job = background_job(cloud=cloud)
    await queues[1].enqueue(job)
    image_entry = (await image_stores[1].read_new(count=1, block_ms=1))[0]
    drawing = asyncio.create_task(queues[1].process_entry(image_entry))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        await queues[0].enqueue(queued_job(jobId="normal-chat", chatTools=()))
        text_entry = (await image_stores[0].read_new(count=1, block_ms=1))[0]
        await asyncio.wait_for(queues[0].process_entry(text_entry), 1)
        assert not drawing.done()
        assert bot.call_api.await_count == 1
        assert "可以继续聊天" in str(bot.call_api.await_args.kwargs["message"])
        async with queues[0].session_guard(job.session_id, wait=False):
            await service.reset(job.session_id)
    finally:
        release.set()
        await drawing
    assert history.context(job.session_id).messages == ()
    assert bot.call_api.await_count == 3
    assert [part.type for part in bot.call_api.await_args_list[1].kwargs["message"]] == ["image"]
    assert [part.type for part in bot.call_api.await_args.kwargs["message"]] == ["at", "text"]
    assert await image_stores[1].source_length() == 0


async def test_image_enqueue_deduplicates_concurrent_retries_after_completion(image_stores):
    """入队响应丢失、并发重试和 ACK 后重试都不会再产生一次绘图。"""

    store = image_stores[1]
    job = background_job()
    ids = await asyncio.gather(*(store.enqueue(job) for _ in range(5)))
    assert len(set(ids)) == 1
    assert await store.source_length() == 1
    with pytest.raises(ValueError, match="parameters changed"):
        await store.enqueue(job.model_copy(update={"prompt": "different"}))
    store._max_pending = 1
    assert await store.enqueue(job) == ids[0]
    with pytest.raises(ConversationQueueFullError):
        await store.enqueue(background_job(job_id="other"))
    await store.read_new(count=1, block_ms=1)
    await store.complete(ids[0])
    assert await store.enqueue(job) == ids[0]
    assert await store.source_length() == 0
    next_id = await store.enqueue(background_job(job_id="next"))
    assert tuple(map(int, next_id.split("-"))) > tuple(map(int, ids[0].split("-")))


async def test_background_image_restart_and_delivery_retry_reuse_result(
    monkeypatch, chat_plugin, image_stores,
):
    """结果保存后重启并遇到图片发送重试，复用图片并继续发送模型完成文案。"""

    store = image_stores[1]
    monkeypatch.setattr(chat_plugin, "_redis_client", store._redis)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_local_workflow_enabled", True)
    workflow = SimpleNamespace(generate=AsyncMock(return_value=GeneratedWorkflowImage(
        media_type="image/png", data=b"\x89PNG\r\n\x1a\nimage",
    )))
    monkeypatch.setattr(chat_plugin, "workflow_service", workflow)
    bot = onebot_mock()
    bot.call_api.side_effect = [ConnectionError("offline"), {"message_id": 1}, {"message_id": 2}]
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    commit = AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "commit_queued", commit)
    completion = AsyncMock(return_value="图片画好啦。")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", completion)
    job = background_job()
    stream_id = await store.enqueue(job)
    await store.read_new(count=1, block_ms=1)
    processor = chat_plugin._ConversationProcessor()
    result = await processor.generate(job)
    # 模拟图片已完成但队列结果还未保存时重试，持久操作仍恢复同一张图片。
    assert await chat_plugin._ConversationProcessor().generate(job) == result
    await store.save_result(stream_id, result)
    replacement = RedisConversationQueueStore(
        store._redis, key_prefix="test:background:image", group_name="workers",
        consumer_name="replacement", max_pending=64, result_ttl_seconds=60,
    )
    await replacement.initialize()
    reclaimed = await replacement.reclaim(count=10, min_idle_ms=0)
    assert len(reclaimed) == 1
    queue = ConversationQueue(
        store=replacement, processor=chat_plugin._ConversationProcessor(),
        policy=QueuePolicy(1, 2, .01, .01, 600), clock=lambda: 1_800_000_000,
    )
    await queue.process_entry(reclaimed[0])
    workflow.generate.assert_awaited_once()
    assert bot.call_api.await_count == 3
    assert bot.call_api.await_args.kwargs["message"].extract_plain_text().strip() == "图片画好啦。"
    completion.assert_awaited_once()
    commit.assert_not_awaited()
    assert await replacement.source_length() == 0


@pytest.mark.parametrize("cloud", [False, True])
@pytest.mark.parametrize("group", [False, True])
@pytest.mark.parametrize("auto_speak", [False, True])
async def test_image_is_sent_before_llm_completion_and_requester_mention(
    monkeypatch, chat_plugin, image_stores, cloud, group, auto_speak,
) -> None:
    """群聊绘图完成始终 @ 创建人，即使任务继承了主动发言的文字限额。"""

    store = image_stores[1]
    monkeypatch.setattr(chat_plugin, "_redis_client", store._redis)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_local_workflow_enabled", True)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_image_generation_enabled", True)
    png = b"\x89PNG\r\n\x1a\nimage"
    workflow = SimpleNamespace(generate=AsyncMock(return_value=GeneratedWorkflowImage(
        media_type="image/png", data=png,
    )))
    monkeypatch.setattr(chat_plugin, "workflow_service", workflow)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_media_queued", AsyncMock(
        return_value=ChatMediaResponse(items=(ChatMediaItem(
            type="IMAGE", mimeType="image/png", data=base64.b64encode(png).decode(),
        ),)),
    ))
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)

    async def complete(*args, **kwargs):
        """模型调用只能发生在图片实际送达之后，且只表达已经确定的完成事实。"""

        assert bot.call_api.await_count == 1
        assert [part.type for part in bot.call_api.await_args.kwargs["message"]] == ["image"]
        assert args[0] != "qq_session_agent"
        assert args[1] == "画一只猫"
        assert "WORKFLOW_STATUS" in kwargs["instruction"]
        assert "completed" in kwargs["instruction"]
        assert kwargs["use_agent"] is True
        assert kwargs["user_name"] == "测试用户"
        return "画好啦，看看这只猫！[CQ:at,qq=10002]"

    completion = AsyncMock(side_effect=complete)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", completion)
    job = background_job(cloud=cloud).model_copy(update={
        "prompt": "画一只猫", "scene": "group" if group else "private",
        "group_id": "20001" if group else None,
        "mention_target_id": "10002" if group else None,
        "chat_context": None,
        "auto_speak_max_reply_chars": 200 if auto_speak else None,
    })
    await store.enqueue(job)
    entry = (await store.read_new(count=1, block_ms=1))[0]
    queue = ConversationQueue(
        store=store, processor=chat_plugin._ConversationProcessor(),
        policy=QueuePolicy(1, 2, .01, .01, 600), clock=lambda: 1_800_000_000,
    )
    await queue.process_entry(entry)
    assert await store.source_length() == 0
    assert bot.call_api.await_count == 2
    notification = bot.call_api.await_args.kwargs["message"]
    assert [part.type for part in notification] == (["at", "text"] if group else ["text"])
    if group:
        assert notification[0].data["qq"] == "10001"
    assert notification.extract_plain_text().strip() == "画好啦，看看这只猫！[CQ:at,qq=10002]"
    completion.assert_awaited_once()


@pytest.mark.parametrize("cloud", [False, True])
@pytest.mark.parametrize("group", [False, True])
async def test_foreground_group_image_notifies_creator_after_delivery(
    monkeypatch, chat_plugin, image_stores, cloud, group,
) -> None:
    """恢复普通绘图结果后，群聊追加创建人完成通知，私聊维持原投递方式。"""

    store = image_stores[0]
    monkeypatch.setattr(chat_plugin, "_redis_client", store._redis)
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    job = queued_job(
        chatTools=(), scene="group" if group else "private",
        groupId="20001" if group else None, chatContext=None,
        mentionTargetId="10002" if group else None,
    )
    png = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()
    result = (
        ConversationResult(kind="MEDIA", media=ChatMediaResponse(items=(
            ChatMediaItem(type="IMAGE", mimeType="image/png", data=png),
        ))) if cloud else ConversationResult(
            kind="WORKFLOW_IMAGES", workflowImages=(
                WorkflowImageArtifact(mimeType="image/png", data=png),
            ),
        )
    )

    async def complete(*_args, **_kwargs):
        """完成事实只能在图片网络投递成功后交给模型表达。"""

        assert bot.call_api.await_count == 1
        assert bot.call_api.await_args.kwargs["message"][0].type == "image"
        return "图片画好了。"

    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", complete)
    monkeypatch.setattr(chat_plugin.chat_service, "commit_queued", AsyncMock())
    stream_id = await store.enqueue(job)
    entry = (await store.read_new(count=1, block_ms=1))[0]
    await store.save_result(stream_id, result)
    queue = ConversationQueue(
        store=store, processor=chat_plugin._ConversationProcessor(),
        policy=QueuePolicy(1, 2, .01, .01, 600), clock=lambda: 1_800_000_000,
    )
    await queue.process_entry(entry)
    assert await store.source_length() == 0
    messages = [call.kwargs["message"] for call in bot.call_api.await_args_list]
    assert [[part.type for part in message] for message in messages] == (
        [["image"], ["at", "text"]] if group else [["image"]]
    )
    if group:
        assert messages[1][0].data["qq"] == "10001"
        assert messages[1].extract_plain_text().strip() == "图片画好了。"


async def test_completion_delivery_restart_reuses_llm_text_without_resending_image(
    monkeypatch, chat_plugin, image_stores,
) -> None:
    """完成文案发送失败后重启，只重发已保存的 LLM 文字，不重画、不重发图片。"""

    store = image_stores[1]
    monkeypatch.setattr(chat_plugin, "_redis_client", store._redis)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_local_workflow_enabled", True)
    workflow = SimpleNamespace(generate=AsyncMock(return_value=GeneratedWorkflowImage(
        media_type="image/png", data=b"\x89PNG\r\n\x1a\nimage",
    )))
    monkeypatch.setattr(chat_plugin, "workflow_service", workflow)
    completion = AsyncMock(return_value="你的猫画好啦。")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", completion)
    bot = onebot_mock()
    bot.call_api.side_effect = [{"message_id": 1}, ConnectionError("offline"), {"message_id": 2}]
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    job = background_job()
    stream_id = await store.enqueue(job)
    await store.read_new(count=1, block_ms=1)
    processor = chat_plugin._ConversationProcessor()
    result = await processor.generate(job)
    await store.save_result(stream_id, result)
    await processor.deliver_part(job, result, 0)
    await store.save_delivery_index(stream_id, 1)
    with pytest.raises(ConnectionError):
        await processor.deliver_part(job, result, 1)
    replacement = RedisConversationQueueStore(
        store._redis, key_prefix="test:background:image", group_name="workers",
        consumer_name="replacement", max_pending=64, result_ttl_seconds=60,
    )
    await replacement.initialize()
    entry = (await replacement.reclaim(count=1, min_idle_ms=0))[0]
    queue = ConversationQueue(
        store=replacement, processor=chat_plugin._ConversationProcessor(),
        policy=QueuePolicy(1, 2, .01, .01, 600), clock=lambda: 1_800_000_000,
    )
    await queue.process_entry(entry)
    assert await replacement.source_length() == 0
    messages = [call.kwargs["message"] for call in bot.call_api.await_args_list]
    assert [[part.type for part in message] for message in messages] == [
        ["image"], ["at", "text"], ["at", "text"],
    ]
    assert messages[1] == messages[2]
    assert messages[2].extract_plain_text().strip() == "你的猫画好啦。"
    completion.assert_awaited_once()
    workflow.generate.assert_awaited_once()


@pytest.mark.parametrize("failure", [None, ChatAiUnavailableError("private detail")])
async def test_completion_llm_failure_keeps_delivered_image_without_fixed_text(
    monkeypatch, chat_plugin, image_stores, failure,
) -> None:
    """完成文案为空或模型失败时保留已送达图片，不发送固定成功或失败话术。"""

    store = image_stores[1]
    monkeypatch.setattr(chat_plugin, "_redis_client", store._redis)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_local_workflow_enabled", True)
    monkeypatch.setattr(chat_plugin, "workflow_service", SimpleNamespace(generate=AsyncMock(
        return_value=GeneratedWorkflowImage(media_type="image/png", data=b"\x89PNG\r\n\x1a\nimage"),
    )))
    completion = AsyncMock(return_value="  ", side_effect=failure)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_reply_queued", completion)
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    await store.enqueue(background_job())
    entry = (await store.read_new(count=1, block_ms=1))[0]
    queue = ConversationQueue(
        store=store, processor=chat_plugin._ConversationProcessor(),
        policy=QueuePolicy(1, 2, .01, .01, 600), clock=lambda: 1_800_000_000,
    )
    await queue.process_entry(entry)
    assert await store.source_length() == 0
    assert bot.call_api.await_count == 1
    assert [part.type for part in bot.call_api.await_args.kwargs["message"]] == ["image"]
    completion.assert_awaited_once()


async def test_slow_completion_llm_does_not_block_same_session_chat(
    monkeypatch, chat_plugin, image_stores,
) -> None:
    """完成通知的 Agent 挂起时图片已送达，原会话仍能使用唯一普通聊天槽位。"""

    entered, release = asyncio.Event(), asyncio.Event()
    requests = []

    async def complete(**kwargs):
        """在外部模型边界挂起通知，验证实际 ChatService 的并发和请求契约。"""

        requests.append(kwargs)
        if kwargs["session_id"].startswith("image:"):
            entered.set()
            await release.wait()
            return "你的画已完成。"
        return "我们继续聊。"

    history = ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60)
    service = ChatService(
        client=SimpleNamespace(complete=complete), store=history, system_prompt="正常聊天",
        max_input_chars=100, cooldown_seconds=0, max_concurrent_requests=1,
        max_concurrent_media_requests=1,
        model_router=ContentModelRouter("text", "vision", "image", "audio"),
    )
    monkeypatch.setattr(chat_plugin, "chat_service", service)
    monkeypatch.setattr(chat_plugin, "_redis_client", image_stores[0]._redis)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_local_workflow_enabled", True)
    monkeypatch.setattr(chat_plugin, "workflow_service", SimpleNamespace(generate=AsyncMock(
        return_value=GeneratedWorkflowImage(media_type="image/png", data=b"\x89PNG\r\n\x1a\nimage"),
    )))
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    processor = chat_plugin._ConversationProcessor()
    queues = tuple(ConversationQueue(
        store=store, processor=processor, policy=QueuePolicy(1, 2, .01, .01, 600),
        clock=lambda: 1_800_000_000,
    ) for store in image_stores)
    await queues[1].enqueue(background_job())
    image_entry = (await image_stores[1].read_new(count=1, block_ms=1))[0]
    drawing = asyncio.create_task(queues[1].process_entry(image_entry))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert [part.type for part in bot.call_api.await_args.kwargs["message"]] == ["image"]
        assert requests[0]["use_agent"] is True
        assert requests[0]["use_web_search"] is False
        assert "chat_tools" not in requests[0]
        await queues[0].enqueue(queued_job(jobId="normal-chat", chatTools=()))
        text_entry = (await image_stores[0].read_new(count=1, block_ms=1))[0]
        await asyncio.wait_for(queues[0].process_entry(text_entry), .5)
        assert not drawing.done()
        assert bot.call_api.await_args.kwargs["message"].extract_plain_text() == "我们继续聊。"
    finally:
        release.set()
        await drawing
    assert bot.call_api.await_count == 3
    notification = bot.call_api.await_args.kwargs["message"]
    assert notification.extract_plain_text().strip() == "你的画已完成。"
    messages = history.context("qq_session_agent").messages
    assert len(messages) == 2
    assert messages[-1].content == "我们继续聊。"


async def test_restored_parent_skips_agent_and_only_commits_start_notice(
    monkeypatch, chat_plugin, image_stores,
):
    """入队后父模型收尾失败或进程重启，不重新生成正文或重复投递开画提示。"""

    monkeypatch.setattr(chat_plugin, "_redis_client", image_stores[0]._redis)
    store = RedisToolOperationStore(
        image_stores[0]._redis, key_prefix=chat_plugin._redis_bootstrap.key_prefix,
        ttl_seconds=60,
    )
    job = queued_job(jobId="submitted-parent", chatTools=("local_workflow_generate_image",))
    await store.begin(job.job_id, "image_start_reply", "a" * 64)
    await store.complete(job.job_id, "image_start_reply", "a" * 64, ReplyEffect(text="我来画。"))

    @asynccontextmanager
    async def session(job):
        """从持久化结果恢复新收集器，模拟真实工具会话初始化。"""

        context = ChatToolContext(
            session_id=job.session_id, user_id=job.user_id,
            allowed_tools=frozenset(job.chat_tools), workflow_collector=WorkflowImageCollector(),
        )
        await context.restore_effect(ImageTaskEffect(task_id="image-task-1"))
        yield ChatToolSessionLease(
            tools=job.chat_tools, token="restored", collector=context.workflow_collector,
        )

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", session)
    generate, submit, commit = AsyncMock(), AsyncMock(), AsyncMock()
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", generate)
    monkeypatch.setattr(chat_plugin.chat_service, "commit_queued", commit)
    monkeypatch.setattr(chat_plugin.image_queue, "enqueue", submit)
    processor = chat_plugin._ConversationProcessor()
    result = await processor.generate(job)
    assert result.kind == "IMAGE_QUEUED"
    assert await processor.delivery_count(job, result) == 0
    await processor.commit(job, result)
    assert commit.await_args.args[2] == "我来画。\n[绘图任务已提交]"
    generate.assert_not_awaited()
    submit.assert_not_awaited()


async def test_disabled_workflow_fails_before_comfy_and_reports_unavailable(
    monkeypatch, chat_plugin, image_stores,
) -> None:
    """禁用目录项不能进入 Comfy，后台提示应表达工作流不可用而非执行失败。"""

    comfy = SimpleNamespace(generate=AsyncMock())
    workflow = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=Mock(
            side_effect=WorkflowNotFoundError("PRIVATE_DATABASE_DETAIL"),
        )), client=comfy,
    )
    monkeypatch.setattr(chat_plugin, "workflow_service", workflow)
    monkeypatch.setattr(chat_plugin, "_redis_client", image_stores[0]._redis)
    monkeypatch.setattr(chat_plugin.plugin_config, "chat_local_workflow_enabled", True)
    composer = SimpleNamespace(compose=AsyncMock(return_value=None))
    monkeypatch.setattr(chat_plugin, "_reply_composer", composer)
    processor = chat_plugin._ConversationProcessor()
    job = background_job()
    with pytest.raises(WorkflowNotFoundError) as unavailable:
        await processor.generate(job)
    await processor.deliver_failure(job, unavailable.value)
    intent = composer.compose.await_args.args[0]
    assert intent.code == "FEATURE_UNAVAILABLE"
    assert intent.facts.feature == "所选本地绘图工作流"
    assert "PRIVATE_DATABASE_DETAIL" not in intent.model_dump_json()
    comfy.generate.assert_not_awaited()


async def test_image_tool_uses_agent_notice_without_second_model_or_waiting_for_comfy(
    monkeypatch, chat_plugin,
) -> None:
    """Comfy 未返回时，父聊天任务也必须已完成提交并释放执行权。"""

    started = asyncio.Event()

    async def slow_image(**_kwargs):
        """模拟长时间运行的外部绘图服务。"""

        started.set()
        await asyncio.Event().wait()

    workflow = TextWorkflowServiceStub(generate=AsyncMock(side_effect=slow_image))
    pending = []

    async def enqueue(job):
        """记录持久化提交边界，不立即执行媒体任务。"""

        pending.append(job)
        return "1-0"

    monkeypatch.setattr(chat_plugin, "image_queue", SimpleNamespace(enqueue=enqueue), raising=False)
    bot = onebot_mock()
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    extra_model = AsyncMock(return_value="我不能画这个。")
    monkeypatch.setattr(chat_plugin.chat_service, "generate_image_start_queued", extra_model)
    context = None

    @asynccontextmanager
    async def session(job):
        """保留真实工具逻辑及其请求级收集器。"""

        nonlocal context
        context = ChatToolContext(
            session_id=job.session_id, user_id=job.user_id,
            allowed_tools=frozenset(job.chat_tools), workflow_service=workflow,
            workflow_collector=WorkflowImageCollector(),
        )
        yield ChatToolSessionLease(
            tools=job.chat_tools, token="test-token", collector=context.workflow_collector,
        )

    async def agent(*_args, **_kwargs):
        """真实调用绘图工具，最终文字不能重复投递。"""

        await build_chat_tool_registry().call(
            "local_workflow_generate_image",
            {"workflow_key": "portrait", "positive_prompt": "a cat",
             "start_reply": "我来画，稍等。"}, context,
        )
        return "已安排"

    monkeypatch.setattr(chat_plugin, "_open_queued_tool_session", session)
    monkeypatch.setattr(chat_plugin.chat_service, "generate_queued", agent)
    job = queued_job(jobId="background-parent", chatTools=("local_workflow_generate_image",))
    result = await asyncio.wait_for(chat_plugin._ConversationProcessor().generate(job), 0.3)
    assert not started.is_set()
    assert len(pending) == 1
    assert pending[0].background_image
    assert pending[0].workflow_request.positive_prompt == "a cat"
    assert result.kind == "IMAGE_QUEUED"
    extra_model.assert_not_awaited()
    assert bot.call_api.await_args.kwargs["message"].extract_plain_text() == "我来画，稍等。"
    assert await chat_plugin._ConversationProcessor().delivery_count(job, result) == 0


async def test_authorized_media_tools_do_not_serialize_unrelated_chat(monkeypatch) -> None:
    """只是向模型提供绘图或 TTS 工具，不应占用唯一媒体生成槽位。"""

    entered = asyncio.Event()
    second = asyncio.Event()
    release = asyncio.Event()
    client = SimpleNamespace(complete=None)

    async def complete(**kwargs):
        """第一个普通回答挂起，第二个独立会话仍可进入模型。"""

        if kwargs["session_id"] == "first":
            entered.set()
            await release.wait()
        else:
            second.set()
        return "普通回答"

    monkeypatch.setattr(client, "complete", complete)
    service = ChatService(
        client=client, store=ConversationStore(history_turns=2, max_sessions=10, ttl_seconds=60),
        system_prompt="正常聊天", max_input_chars=100, cooldown_seconds=0,
        max_concurrent_requests=2, max_concurrent_media_requests=1,
        model_router=ContentModelRouter("text", "vision", "image", "audio"),
    )
    kwargs = dict(chat_tools=("local_workflow_generate_image", "tts_generate"),
                  tool_session_token="token", request_id="req")
    first = asyncio.create_task(service.generate_queued("first", "你好", **kwargs))
    await asyncio.wait_for(entered.wait(), 1)
    other = asyncio.create_task(service.generate_queued("second", "你好", **kwargs))
    try:
        await asyncio.wait_for(second.wait(), .3)
    finally:
        release.set()
        await asyncio.gather(first, other)
