"""工具原图归属、幂等恢复和不向模型开放路径。"""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_workflow_inputs import i2i_definition, uploaded_image

from agent_bot.services.chat_tools import (
    ChatToolContext,
    ChatToolError,
    WorkflowGenerateArguments,
    build_chat_tool_registry,
)
from agent_bot.services.tool_operations import ImageTaskEffect
from agent_bot.services.workflow_execution import (
    WorkflowExecutionService,
    WorkflowImageCollector,
    WorkflowImageRequest,
    WorkflowSummary,
)
from agent_bot.services.workflow_inputs import workflow_input_scope


@pytest.mark.parametrize("urls,materials,with_source", [
    ((), (), False), (("https://image.invalid/a",), (), True),
    (("https://image.invalid/a", "https://image.invalid/a"), (), True),
    (("https://image.invalid/a", "https://image.invalid/b"), (), False),
    ((), ("a" * 32,), True), (("https://image.invalid/a",), ("a" * 32,), False),
])
async def test_workflow_catalog_only_offers_usable_image_inputs(urls, materials, with_source):
    """工作流列表按本次真实输入过滤，无原图仍保留固定底图且不暴露不可执行的双图流程。"""
    summaries = (
        WorkflowSummary("text", "文生图", ""),
        WorkflowSummary("fixed", "固定底图", "", "fixed_reference", False),
        WorkflowSummary("dual", "双图参考", "", "image_to_image", True),
        WorkflowSummary("video", "图生视频", "", "image_to_video", True),
    )
    context = ChatToolContext(
        session_id="s", user_id="1", allowed_tools=frozenset({"local_workflow_list"}),
        workflow_service=SimpleNamespace(list_workflows=AsyncMock(return_value=summaries)),
        image_urls=urls, image_material_ids=materials,
    )
    result = await build_chat_tool_registry().call("local_workflow_list", {}, context)
    keys = [item["key"] for item in json.loads(result.content)["workflows"]]
    assert keys == (["text", "fixed", "dual", "video"] if with_source else ["text", "fixed"])


async def test_missing_original_is_rejected_before_effect_and_keeps_generation_allowance():
    """错误选择不上传、不占绘图额度或持久副作用，模型随后仍可明确选取固定底图。"""
    summaries = {
        "dual": WorkflowSummary("dual", "双图参考", "", "image_to_image", True),
        "fixed": WorkflowSummary("fixed", "固定底图", "", "fixed_reference", False),
    }
    service = SimpleNamespace(
        describe_workflow=AsyncMock(side_effect=lambda key: summaries[key]),
        prepare_request=AsyncMock(return_value=WorkflowImageRequest(
            workflow_key="fixed", positive_prompt="人物坐姿", fixed_reference=True,
            workflow_sha256="c" * 64,
        )),
    )
    operations = SimpleNamespace(begin=AsyncMock(return_value=None), complete=AsyncMock())
    collector = WorkflowImageCollector()
    collector.submit = AsyncMock(return_value="image-task")
    context = ChatToolContext(
        session_id="s", user_id="1", job_id="parent", operation_store=operations,
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=service, workflow_collector=collector,
    )
    registry = build_chat_tool_registry()
    args = {"workflow_key": "dual", "positive_prompt": "人物坐姿", "start_reply": "我来画。"}
    with pytest.raises(ChatToolError) as failure:
        await registry.call("local_workflow_generate_image", args, context)
    assert failure.value.code == "WORKFLOW_INPUT_REQUIRED"
    service.prepare_request.assert_not_awaited()
    operations.begin.assert_not_awaited()
    collector.submit.assert_not_awaited()
    result = await registry.call("local_workflow_generate_image", {**args, "workflow_key": "fixed"},
                                 context)
    assert json.loads(result.content) == {"status": "queued", "task_id": "image-task"}
    collector.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_i2i_tool_uses_received_material_original():
    """已接收素材直接上传原件给工作流，不再下载远程链接。"""
    from contextlib import asynccontextmanager
    from io import BytesIO

    from agent_bot.media.contracts import MediaOwner

    definition = i2i_definition()
    scope = workflow_input_scope("material-parent")
    uploaded = uploaded_image(subfolder=f"chat-i2i/{scope}")
    client = SimpleNamespace(
        upload_input_bytes=AsyncMock(return_value=uploaded),
        upload_input_image=AsyncMock(side_effect=AssertionError("不得下载远程图片")),
    )

    @asynccontextmanager
    async def content(id, *, owner):
        """只替换原件文件 IO，真实工作流仍处理上传和图生图请求。"""
        assert id == "a" * 32 and owner == MediaOwner(entry="qq", session_id="shared")
        yield BytesIO(b"original-image")

    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    materials = SimpleNamespace(
        open_content=content,
        describe=AsyncMock(return_value=SimpleNamespace(mime_type="image/png")),
    )
    collector = WorkflowImageCollector()
    collector.submit = AsyncMock(return_value="image-" + scope)
    context = ChatToolContext(
        session_id="shared", user_id="1",
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=service, workflow_collector=collector, workflow_request_scope=scope,
        image_material_ids=("a" * 32,), material_store=materials,
    )
    await build_chat_tool_registry().call("local_workflow_generate_image", {
        "workflow_key": "qwen-i2i", "positive_prompt": "改蓝色", "start_reply": "我来改。",
    }, context)
    request = collector.submit.await_args.args[0]
    assert request.input_image == uploaded
    assert client.upload_input_bytes.await_args.args == (b"original-image",)


@pytest.mark.asyncio
async def test_i2i_tool_binds_job_image_and_restores_without_upload():
    definition = i2i_definition()
    scope = workflow_input_scope("parent")
    uploaded = uploaded_image(subfolder=f"chat-i2i/{scope}")
    client = SimpleNamespace(upload_input_image=AsyncMock(return_value=uploaded))
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    operations = SimpleNamespace(begin=AsyncMock(return_value=None), complete=AsyncMock())
    collector = WorkflowImageCollector()
    collector.submit = AsyncMock(return_value="image-" + scope)
    context = ChatToolContext(
        session_id="shared", user_id="1", job_id="parent", operation_store=operations,
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=service, workflow_collector=collector,
        image_urls=("https://gchat.qpic.cn/source",), workflow_request_scope=scope,
    )
    args = {"workflow_key": "qwen-i2i", "positive_prompt": "改蓝色", "start_reply": "我来改。"}
    registry = build_chat_tool_registry()
    result = await registry.call("local_workflow_generate_image", args, context)
    assert json.loads(result.content)["status"] == "queued"
    request = collector.submit.await_args.args[0]
    assert request.input_image == uploaded
    assert request.workflow_sha256 == definition.execution_sha256
    signature = operations.begin.await_args.args[2]
    assert "source" not in signature
    operations.begin.return_value = ImageTaskEffect(task_id="image-" + scope)
    await registry.call("local_workflow_generate_image", args, context)
    client.upload_input_image.assert_awaited_once()
    collector.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_t2i_tool_signature_still_restores_existing_effect():
    arguments = {"workflow_key": "portrait", "positive_prompt": "cat", "start_reply": "我来画。"}
    expected = hashlib.sha256(
        WorkflowGenerateArguments.model_validate(arguments).model_dump_json().encode(),
    ).hexdigest()
    operations = SimpleNamespace(
        begin=AsyncMock(return_value=ImageTaskEffect(task_id="image-old")),
    )
    service = SimpleNamespace(
        describe_workflow=AsyncMock(return_value=WorkflowSummary("portrait", "P", "")),
        prepare_request=AsyncMock(side_effect=AssertionError),
    )
    context = ChatToolContext(
        session_id="shared", user_id="1", job_id="parent", operation_store=operations,
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=service, workflow_collector=WorkflowImageCollector(),
    )
    await build_chat_tool_registry().call("local_workflow_generate_image", arguments, context)
    operations.begin.assert_awaited_once_with("parent", "local_workflow_generate_image", expected)
    service.prepare_request.assert_not_called()


def test_llm_cannot_supply_image_path_and_contexts_have_separate_scopes():
    assert not {"input_image", "image_url", "path"} & WorkflowGenerateArguments.model_fields.keys()
    first = ChatToolContext(session_id="same", user_id="1", allowed_tools=frozenset())
    second = ChatToolContext(session_id="same", user_id="1", allowed_tools=frozenset())
    assert first.workflow_request_scope != second.workflow_request_scope
