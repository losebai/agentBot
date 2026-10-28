"""生成结果进入持久素材库，失败重试复用原图且使用真实入口会话。"""

import base64
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.materials.contracts import MaterialSearchArguments
from agent_bot.media.contracts import MediaOwner
from agent_bot.media.errors import MediaError
from agent_bot.services.chat_tools import ChatToolContext, ChatToolError, build_chat_tool_registry
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.services.workflow_execution import (
    GeneratedWorkflowImage,
    WorkflowImageCollector,
    WorkflowImageRequest,
    WorkflowSummary,
)
from materials.test_store import material_database as material_database
from materials.test_store import materials as materials
from media.test_admin_tools import adapters, workflow_call
from media.test_inputs import picture
from media.test_store import SCOPE, storage


async def test_admin_workflow_keeps_generated_material_without_sending(
    materials, media_database, agent_params, tmp_path, media_clock,
):
    """管理工作流没有 QQ 投递，成功后原件仍按服务器绑定的管理会话保存。"""
    async with storage(media_database, agent_params, tmp_path / "media", media_clock) as store:
        tools, _, _, _ = adapters(store, tmp_path)
        tools.resources = SimpleNamespace(
            readiness=SimpleNamespace(storage_available=True), materials=materials,
        )
        await tools.bind(SCOPE, ()).call(workflow_call())
        records = (await materials.search(SCOPE.owner, MaterialSearchArguments())).records
        assert len(records) == 1 and records[0].categories == ("generated",)
        async with materials.open_content(records[0].id, owner=SCOPE.owner) as file:
            assert file.read() == picture()


@pytest.mark.parametrize("save_fails", [False, True])
async def test_background_workflow_material_retry_never_regenerates(materials, save_fails):
    """素材失败可由持久生成回执补存，Comfy 不重复计费，原件始终归原 QQ 会话。"""
    effects = []

    async def begin(*args):
        """模拟已经提交的生成回执，素材发布不属于上游副作用。"""
        return effects[0] if effects else None

    async def complete(*args):
        """在素材保存前保留生成结果，供失败后的请求恢复。"""
        effects.append(args[-1])

    service = SimpleNamespace(generate=AsyncMock(return_value=GeneratedWorkflowImage(
        media_type="image/png", data=picture(),
    )))
    processor = ConversationQueueProcessor(
        config_provider=lambda: SimpleNamespace(chat_local_workflow_enabled=True),
        chat_service_provider=lambda: None, runtime_lease=None, tool_session_provider=None,
        bot_provider=None, reply_composer=None, reply_gateway=None,
        operation_store_provider=lambda: SimpleNamespace(begin=begin, complete=complete),
        image_submitter=None, workflow_service_provider=lambda: service,
        material_store_provider=lambda: materials,
    )
    job = ConversationJob(
        jobId="drawing", requestId="draw-request", createdAt=time.time(),
        sessionId="qq-real-session", botId="90001", scene="private", userId="10001",
        prompt="猫", backgroundImage=True,
        workflowRequest=WorkflowImageRequest(workflow_key="portrait", positive_prompt="cat"),
    )
    if save_fails:
        put = materials.put
        materials.put = AsyncMock(side_effect=MediaError("MEDIA_SAVE_FAILED"))
        with pytest.raises(MediaError, match="MEDIA_SAVE_FAILED"):
            await processor._generate_workflow(job)
        materials.put = put
    result = await processor._generate_workflow(job)
    assert base64.b64decode(result.workflow_images[0].data) == picture()
    owner = MediaOwner(entry="qq", session_id="qq-real-session")
    records = (await materials.search(owner, MaterialSearchArguments())).records
    assert len(records) == 1 and records[0].categories == ("generated",)
    service.generate.assert_awaited_once()


@pytest.mark.parametrize("save_fails", [False, True])
async def test_synchronous_qq_workflow_keeps_generated_material(materials, save_fails):
    """同步工具不能依赖未来的 QQ 发送来保存生成原件。"""
    workflow = SimpleNamespace(
        describe_workflow=AsyncMock(return_value=WorkflowSummary("portrait", "人像", "")),
        prepare_request=AsyncMock(return_value=WorkflowImageRequest(
            workflow_key="portrait", positive_prompt="cat",
        )),
        generate=AsyncMock(return_value=GeneratedWorkflowImage(
            media_type="image/png", data=picture(),
        )),
    )
    effects = []

    async def begin(*args):
        """恢复工具回执，素材发布失败不能重复生成。"""
        return effects[0] if effects else None

    async def complete(*args):
        """缓存上游已成功返回的原图，素材事务独立恢复。"""
        effects.append(args[-1])

    context = ChatToolContext(
        session_id="qq-sync", user_id="10001",
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=workflow, workflow_collector=WorkflowImageCollector(),
        material_store=materials,
        job_id="sync-image", operation_store=SimpleNamespace(begin=begin, complete=complete),
    )
    call = workflow_call()
    if save_fails:
        put = materials.put
        materials.put = AsyncMock(side_effect=MediaError("MEDIA_SAVE_FAILED"))
        with pytest.raises(ChatToolError):
            await build_chat_tool_registry().call(call.name, call.arguments, context)
        materials.put = put
        context.workflow_collector = WorkflowImageCollector()
    await build_chat_tool_registry().call(call.name, call.arguments, context)
    owner = MediaOwner(entry="qq", session_id="qq-sync")
    records = (await materials.search(owner, MaterialSearchArguments())).records
    assert len(records) == 1 and records[0].categories == ("generated",)
    workflow.generate.assert_awaited_once()
