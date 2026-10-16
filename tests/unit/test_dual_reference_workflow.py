"""双图编辑只替换用户画布，管理员身份底图与排队版本保持受控。"""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_custom_sampler_workflow import custom_definition
from test_fixed_reference_workflow import REFERENCE
from test_workflow_inputs import uploaded_image

from agent_bot.services.workflow_execution import (
    GeneratedWorkflowImage,
    WorkflowExecutionError,
    WorkflowExecutionService,
)
from agent_bot.services.workflows import (
    WorkflowDefinition,
    WorkflowValidationError,
    prepare_workflow_prompt,
    validate_workflow_bindings,
)


def dual_definition() -> WorkflowDefinition:
    """上传图位于首个参考槽，固定身份图位于第二槽并直接使用编辑指令。"""
    definition = custom_definition()
    api = json.loads(definition.api_json)
    api["1"]["inputs"]["image"] = "upload-placeholder.png"
    api["22"] = {"class_type": "LoadImage", "inputs": {"image": REFERENCE}}
    api["12"]["inputs"]["images.image_2"] = ["22", 0]
    api["12"]["inputs"]["resolution"] = 1024
    return replace(definition, workflow_type="image_to_image", api_json=json.dumps(api))


def test_dual_requests_replace_only_upload_and_keep_identity_reference() -> None:
    """连续上传不能串图、覆盖身份底图或改变决定输出画幅的首图连接。"""
    definition = dual_definition()
    original = json.loads(definition.api_json)
    first, second = uploaded_image(), uploaded_image(subfolder="chat-i2i/" + "c" * 32)
    for seed, image in enumerate((first, second)):
        result = prepare_workflow_prompt(
            definition, positive_prompt="让第二张图的桃桃采用第一张图的动作",
            negative_prompt="", seed=seed, input_image=image,
        )
        assert result["1"]["inputs"]["image"] == image.comfy_path
        assert result["22"]["inputs"]["image"] == REFERENCE
        assert result["12"]["inputs"]["images.image_1"] == ["1", 0]
        assert result["12"]["inputs"]["images.image_2"] == ["22", 0]
        assert result["14"]["inputs"]["noise_seed"] == seed
    assert json.loads(definition.api_json) == original


@pytest.mark.parametrize("path", [
    "private.png", "../private.png", "https://example.com/a.png",
    "chat-i2i/" + "a" * 32 + "/" + "b" * 64 + ".png",
])
def test_dual_reference_rejects_uncontrolled_identity_image(path: str) -> None:
    """第二张图必须由管理员绑定，不能绕过上传边界读取其他图片。"""
    definition = dual_definition()
    api = json.loads(definition.api_json)
    api["22"]["inputs"]["image"] = path
    with pytest.raises(WorkflowValidationError):
        validate_workflow_bindings(replace(definition, api_json=json.dumps(api)))


@pytest.mark.parametrize("change", [
    "duplicate_reference", "wrong_slot", "extra_image", "extra_loader", "fixed_mode",
])
def test_dual_reference_rejects_ambiguous_image_roles(change: str) -> None:
    """仅允许一个上传画布和一个明确连接的固定身份图，不能互换或暗加图片。"""
    definition = dual_definition()
    api = json.loads(definition.api_json)
    if change == "duplicate_reference":
        api["12"]["inputs"]["images.image_2"] = ["1", 0]
    elif change == "wrong_slot":
        api["12"]["inputs"]["images.image_2"] = ["22", 1]
    elif change == "extra_image":
        api["12"]["inputs"]["images.image_3"] = ["22", 0]
    elif change == "extra_loader":
        api["23"] = {"class_type": "LoadImage", "inputs": {"image": REFERENCE}}
    else:
        api["1"]["inputs"]["image"] = REFERENCE
        definition = replace(definition, workflow_type="fixed_reference")
    with pytest.raises(WorkflowValidationError):
        validate_workflow_bindings(replace(definition, api_json=json.dumps(api)))


@pytest.mark.asyncio
async def test_dual_service_requires_upload_and_generates_with_both_images() -> None:
    """实际准备与生成链路仍只上传一张用户图，并将两张图交给 Comfy。"""
    definition = dual_definition()
    image = uploaded_image()
    client = SimpleNamespace(
        upload_input_image=AsyncMock(return_value=image),
        generate=AsyncMock(return_value=GeneratedWorkflowImage(
            media_type="image/png", data=b"generated",
        )),
    )
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    summary = await service.describe_workflow(definition.workflow_key)
    assert summary.requires_image
    with pytest.raises(WorkflowExecutionError, match="原图"):
        await service.prepare_request(
            workflow_key=definition.workflow_key, positive_prompt="照原图动作画桃桃",
            negative_prompt="", request_scope="a" * 32,
        )
    request = await service.prepare_request(
        workflow_key=definition.workflow_key, positive_prompt="照原图动作画桃桃",
        negative_prompt="", image_urls=("https://example.com/input.png",),
        request_scope="a" * 32,
    )
    assert request.input_image == image and not request.fixed_reference
    result = await service.generate(
        workflow_key=request.workflow_key, positive_prompt=request.positive_prompt,
        negative_prompt=request.negative_prompt, input_image=request.input_image,
        workflow_sha256=request.workflow_sha256,
    )
    assert result.data == b"generated"
    prompt = client.generate.call_args.args[0]
    assert prompt["1"]["inputs"]["image"] == image.comfy_path
    assert prompt["22"]["inputs"]["image"] == REFERENCE


@pytest.mark.asyncio
async def test_dual_queue_rejects_changed_identity_reference() -> None:
    """排队期间修改桃桃底图必须拒绝旧任务，不能悄悄生成另一人物。"""
    definition = dual_definition()
    snapshot = definition.execution_sha256
    api = json.loads(definition.api_json)
    api["22"]["inputs"]["image"] = REFERENCE.replace("d" * 64, "e" * 64)
    changed = replace(definition, api_json=json.dumps(api))
    client = SimpleNamespace(generate=AsyncMock())
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: changed), client=client,
    )
    with pytest.raises(WorkflowExecutionError, match="已更新"):
        await service.generate(
            workflow_key=changed.workflow_key, positive_prompt="照原图动作画桃桃",
            negative_prompt="", input_image=uploaded_image(), workflow_sha256=snapshot,
        )
    client.generate.assert_not_called()
