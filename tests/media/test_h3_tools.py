"""管理视频附件和 QQ 持久化引用不得退化成图片或大段 Base64。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.agent.contracts import ToolCall
from agent_bot.media.contracts import MediaAssetView, MediaOwner
from agent_bot.services.workflow_execution import GeneratedWorkflowVideo, WorkflowSummary
from media.test_admin_tools import adapters
from media.test_store import SCOPE, storage
from unit.test_comfy_video import MP4


async def test_admin_h3_requires_owned_image_and_saves_video_attachment(
    media_database,
    agent_params,
    tmp_path,
    media_clock,
):
    """独立视频工具只接收当前执行原图并将结果关联当前执行。"""
    async with storage(media_database, agent_params, tmp_path / "media", media_clock) as store:
        tools, workflow, _, _ = adapters(store, tmp_path)
        workflow.describe_workflow.return_value = WorkflowSummary(
            "h3", "H3", "", "image_to_video", True
        )
        workflow.generate_video = AsyncMock(return_value=GeneratedWorkflowVideo("video/mp4", MP4))
        # 上游上传引用由真实 prepare_request 路径的独立测试覆盖。
        call = ToolCall(
            call_id="video",
            name="local_workflow_generate_video",
            arguments={
                "workflow_key": "h3",
                "positive_prompt": "缓慢推镜",
                "start_reply": "开始制作视频",
            },
        )
        image = SimpleNamespace(media_type="image/png", data=b"original")
        session = tools.bind(SCOPE, (image,))
        result = await session.call(call)
        assert result.attachments[0].mime_type == "video/mp4"
        asset = result.attachments[0]
        async with store.open_content(asset.id, owner=SCOPE.owner) as handle:
            assert handle.read() == MP4
        assert (await store.describe(asset.id, owner=SCOPE.owner)).size_bytes == len(MP4)
        workflow.generate_video.assert_awaited_once()


def test_video_metadata_has_separate_budget_without_relaxing_image_limit():
    """视频为 100 MiB，图片仍是 10 MiB；权限归属不会进入公开元数据。"""
    from datetime import datetime, timedelta

    now = datetime.now()
    values = dict(
        id="a" * 32,
        purpose="output",
        size_bytes=11 * 1024 * 1024,
        sha256="b" * 64,
        status="ready",
        created_at=now,
        expires_at=now + timedelta(days=1),
    )
    assert MediaAssetView(**values, mime_type="video/mp4").size_bytes > 10 * 1024 * 1024
    with pytest.raises(ValueError):
        MediaAssetView(**values, mime_type="image/png")
    assert MediaOwner(entry="qq", session_id="private:123").entry == "qq"


async def test_background_video_retry_reuses_asset_and_delivers_video(
    monkeypatch,
    chat_plugin,
    media_database,
    agent_params,
    tmp_path,
    media_clock,
):
    """重试复用已保存产物，队列只存媒体 ID；OneBot 只在发送阶段读取文件。"""
    import fakeredis.aioredis

    from agent_bot.integrations.tool_operations import RedisToolOperationStore
    from agent_bot.services.conversation_queue import ConversationJob
    from agent_bot.services.workflow_execution import WorkflowImageRequest
    from agent_bot.services.workflow_inputs import WorkflowInputImage

    image = WorkflowInputImage(
        filename="a" * 64 + ".png",
        subfolder="chat-i2i/" + "b" * 32,
        media_type="image/png",
        size_bytes=100,
        sha256="a" * 64,
    )
    job = ConversationJob(
        jobId="image-" + "b" * 32,
        requestId="h3-request",
        createdAt=1,
        sessionId="qq:private:20000",
        botId="10000",
        userId="20000",
        prompt="制作视频",
        scene="private",
        chatTools=(),
        backgroundImage=True,
        workflowRequest=WorkflowImageRequest(
            workflow_key="h3",
            positive_prompt="推镜",
            input_image=image,
            workflow_sha256="a" * 64,
            output_kind="video",
        ),
    )
    redis = fakeredis.aioredis.FakeRedis()
    operations = RedisToolOperationStore(redis, key_prefix="h3-video", ttl_seconds=60)
    service = SimpleNamespace(
        generate_video=AsyncMock(return_value=GeneratedWorkflowVideo("video/mp4", MP4))
    )
    bot = AsyncMock()
    monkeypatch.setattr(
        chat_plugin,
        "plugin_config",
        chat_plugin.plugin_config.model_copy(update={"chat_local_workflow_enabled": True}),
    )
    monkeypatch.setattr(chat_plugin, "workflow_service", service)
    monkeypatch.setattr(chat_plugin, "_queued_bot", lambda _: bot)
    monkeypatch.setattr(chat_plugin, "_tool_operations", lambda: operations)
    async with storage(media_database, agent_params, tmp_path / "media", media_clock) as store:
        processor = chat_plugin._ConversationProcessor()
        processor._media_store_provider = lambda: store
        first = await processor._generate_workflow(job)
        second = await processor._generate_workflow(job)
        assert first == second
        assert first.kind == "WORKFLOW_VIDEO"
        assert "base64" not in first.model_dump_json()
        assert len(first.model_dump_json()) < 1500
        service.generate_video.assert_awaited_once()
        await processor.deliver_part(job, second, 0)
        sent = bot.call_api.await_args or bot.send_private_msg.await_args
        assert sent.kwargs["message"][0].type == "video"
    await redis.aclose()
