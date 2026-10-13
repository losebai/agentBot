"""固定底图与用户传图分离，防止底图替换和后台任务串模板。"""

import json
from dataclasses import asdict, replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from test_workflow_inputs import i2i_definition, uploaded_image, write_i2i_pair

from agent_bot.integrations.workflow_store import _row_to_definition
from agent_bot.services.workflow_execution import (
    GeneratedWorkflowImage,
    WorkflowExecutionError,
    WorkflowExecutionService,
    WorkflowImageRequest,
)
from agent_bot.services.workflows import (
    WorkflowCatalogImporter,
    WorkflowValidationError,
    prepare_workflow_prompt,
    validate_workflow_bindings,
)

REFERENCE = "chat-reference/" + "d" * 64 + ".png"


def fixed_definition():
    """从已验证 Qwen 链路创建管理员固定底图模板。"""
    definition = i2i_definition()
    api = json.loads(definition.api_json)
    api["1"]["inputs"]["image"] = REFERENCE
    api["12"]["inputs"]["prompt"] = ["2", 0]
    for key in ("3", "4", "5", "6", "7", "8", "17"):
        api.pop(key, None)
    return replace(definition, workflow_type="fixed_reference", api_json=json.dumps(api))


def test_fixed_prompt_keeps_reference_and_rejects_uploaded_override():
    """缺少用户图片不应失败，提供上传引用也不能覆盖管理员底图。"""
    definition = fixed_definition()
    result = prepare_workflow_prompt(
        definition, positive_prompt="换成雪景", negative_prompt="模糊", seed=42,
    )
    assert result["1"]["inputs"]["image"] == REFERENCE
    assert result["2"]["inputs"]["value"] == "换成雪景"
    assert result["12"]["inputs"]["negative_prompt"] == "模糊"
    with pytest.raises(WorkflowValidationError):
        prepare_workflow_prompt(
            definition, positive_prompt="换背景", negative_prompt="", seed=42,
            input_image=uploaded_image(),
        )


@pytest.mark.parametrize("path", [
    "sample.png", "../outside.png", "C:/secret.png", "https://example.com/a.png",
    "chat-reference/../a.png", "chat-reference/" + "d" * 64 + ".png [output]",
])
def test_fixed_reference_rejects_uncontrolled_paths(path):
    """固定底图只接受受控目录内按摘要命名的图片。"""
    definition = fixed_definition()
    api = json.loads(definition.api_json)
    api["1"]["inputs"]["image"] = path
    with pytest.raises(WorkflowValidationError):
        validate_workflow_bindings(replace(definition, api_json=json.dumps(api)))


@pytest.mark.asyncio
async def test_fixed_prepare_without_user_image_pins_snapshot_and_never_uploads():
    """用户附件不能取代固定底图；列表不能要求重新传图。"""
    definition = fixed_definition()
    client = SimpleNamespace(upload_input_image=AsyncMock(side_effect=AssertionError))
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition,
                              list_executable=lambda **_: (definition,)), client=client,
    )
    for urls in ((), ("https://gchat.qpic.cn/other",)):
        request = await service.prepare_request(
            workflow_key=definition.workflow_key, positive_prompt="换雪景", negative_prompt="",
            image_urls=urls, request_scope="a" * 32,
        )
        assert request.input_image is None
        assert request.fixed_reference is True
        assert request.workflow_sha256 == definition.execution_sha256
        assert WorkflowImageRequest.model_validate_json(request.model_dump_json()) == request
    summary = await service.describe_workflow(definition.workflow_key)
    assert not summary.requires_image
    assert summary.workflow_type == "fixed_reference"
    assert (await service.list_workflows()) == (summary,)
    client.upload_input_image.assert_not_called()


@pytest.mark.asyncio
async def test_fixed_generate_uses_pinned_template():
    """实际提交给 Comfy 的图片路径必须来自固定模板。"""
    definition = fixed_definition()
    client = SimpleNamespace(generate=AsyncMock(return_value=GeneratedWorkflowImage(
        media_type="image/png", data=b"image",
    )))
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    result = await service.generate(
        workflow_key=definition.workflow_key, positive_prompt="换雪景", negative_prompt="",
        workflow_sha256=definition.execution_sha256,
    )
    assert result.data == b"image"
    prompt = client.generate.call_args.args[0]
    assert prompt["1"]["inputs"]["image"] == REFERENCE
    assert prompt["2"]["inputs"]["value"] == "换雪景"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing_hash", "reference", "user_image"])
async def test_fixed_generate_rejects_unpinned_or_replaced_reference(change):
    """排队后更换底图或伪造上传引用必须停止，不得重新猜测模板。"""
    definition = fixed_definition()
    snapshot = definition.execution_sha256
    if change == "reference":
        api = json.loads(definition.api_json)
        api["1"]["inputs"]["image"] = REFERENCE.replace("d" * 64, "e" * 64)
        definition = replace(definition, api_json=json.dumps(api))
    client = SimpleNamespace(generate=AsyncMock())
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    with pytest.raises(WorkflowExecutionError):
        await service.generate(
            workflow_key=definition.workflow_key, positive_prompt="换雪景", negative_prompt="",
            workflow_sha256=None if change == "missing_hash" else snapshot,
            input_image=uploaded_image() if change == "user_image" else None,
        )
    client.generate.assert_not_called()


@pytest.mark.parametrize("fields", [
    {"fixed_reference": True},
    {"fixed_reference": True, "workflow_sha256": "a" * 64, "input_image": uploaded_image()},
    {"workflow_sha256": "a" * 64},
])
def test_background_request_rejects_inconsistent_reference_modes(fields):
    """固定图必须有摘要且不得携带用户上传，普通图仍严格配对。"""
    with pytest.raises(ValidationError):
        WorkflowImageRequest(workflow_key="taotao", positive_prompt="雪景", **fields)


def test_database_fixed_reference_round_trip():
    """数据库读取不能把固定底图误归为文生图或拒绝全部工作流列表。"""
    row = asdict(fixed_definition())
    row["enabled"] = 1
    restored = _row_to_definition(row)
    validate_workflow_bindings(restored)
    assert restored.workflow_type == "fixed_reference"


def test_old_text_request_retains_legacy_signature_representation():
    """旧任务重启恢复时必须命中原来的已完成图片操作记录。"""
    request = WorkflowImageRequest.model_validate_json(
        '{"workflow_key":"portrait","positive_prompt":"cat","negative_prompt":""}',
    )
    assert request.model_dump_json(exclude_none=True) == (
        '{"workflow_key":"portrait","positive_prompt":"cat","negative_prompt":""}'
    )


def test_fixed_import_retains_bound_reference_and_defaults_disabled(tmp_path):
    """同步不会丢失固定底图类型，也不会擅自启用新工作流。"""
    api = write_i2i_pair(tmp_path)
    api["1"]["inputs"]["image"] = REFERENCE
    api["12"]["inputs"]["prompt"] = ["2", 0]
    removed = {3, 4, 5, 6, 7, 8, 17}
    for key in removed:
        api.pop(str(key), None)
    (tmp_path / "api/edit-api.json").write_text(json.dumps(api), encoding="utf-8")
    ui_path = tmp_path / "edit.json"
    ui = json.loads(ui_path.read_text(encoding="utf-8"))
    next(node for node in ui["nodes"] if node["id"] == 1)["widgets_values"][0] = REFERENCE
    ui["nodes"] = [node for node in ui["nodes"] if node["id"] not in removed]
    ui["links"] = [
        link for link in ui["links"] if link[1] not in removed and link[3] not in removed
    ]
    encoder_ports = next(node for node in ui["nodes"] if node["id"] == 12)["inputs"]
    prompt_slot = next(i for i, port in enumerate(encoder_ports) if port["name"] == "prompt")
    prompt_port = encoder_ports[prompt_slot]
    prompt_port["link"] = 100
    ui["links"].append([100, 2, 0, 12, prompt_slot, "*"])
    ui_path.write_text(json.dumps(ui), encoding="utf-8")
    meta_path = tmp_path / "edit.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["workflow_bindings"]["workflow_type"] = "fixed_reference"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    (definition,) = WorkflowCatalogImporter().scan(tmp_path)
    assert definition.workflow_type == "fixed_reference"
    assert not definition.enabled
    assert json.loads(definition.api_json)["1"]["inputs"]["image"] == REFERENCE


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_fixed_tool_preserves_snapshot_without_user_image(background):
    """工具同步生成和后台入队都必须保留模板摘要。"""
    from agent_bot.services.chat_tools import ChatToolContext, build_chat_tool_registry
    from agent_bot.services.workflow_execution import WorkflowImageCollector

    definition = fixed_definition()
    client = SimpleNamespace(generate=AsyncMock(return_value=GeneratedWorkflowImage(
        media_type="image/png", data=b"image",
    )))
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    collector = WorkflowImageCollector()
    if background:
        collector.submit = AsyncMock(return_value="image-fixed")
    context = ChatToolContext(
        session_id="same", user_id="1",
        allowed_tools=frozenset({"local_workflow_generate_image"}),
        workflow_service=service, workflow_collector=collector,
    )
    result = await build_chat_tool_registry().call("local_workflow_generate_image", {
        "workflow_key": definition.workflow_key, "positive_prompt": "换雪景",
        "start_reply": "我开始画了。",
    }, context)
    if background:
        assert json.loads(result.content)["status"] == "queued"
        request = collector.submit.await_args.args[0]
        assert request.workflow_sha256 == definition.execution_sha256
        assert request.fixed_reference and request.input_image is None
        client.generate.assert_not_called()
    else:
        assert json.loads(result.content)["image_index"] == 1
        assert client.generate.call_args.args[0]["1"]["inputs"]["image"] == REFERENCE
