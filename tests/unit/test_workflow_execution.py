from __future__ import annotations

import json
import logging
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from test_workflow_inputs import i2i_definition, uploaded_image

from agent_bot.services.workflow_execution import (
    WorkflowExecutionError,
    WorkflowExecutionService,
    WorkflowSummary,
)
from agent_bot.services.workflows import WorkflowDefinition


def _text_definition() -> WorkflowDefinition:
    """构造绑定与 API 节点一致的最小文生图工作流。"""

    return WorkflowDefinition(
        workflow_key="portrait",
        display_name="Portrait",
        source_file="portrait.json",
        source_format="api",
        ui_json=None,
        api_json=json.dumps(
            {
                "1": {"class_type": "CheckpointLoaderSimple", "inputs": {}},
                "2": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
                "3": {"class_type": "CLIPTextEncode", "inputs": {"text": "old"}},
                "4": {"class_type": "EmptyLatentImage", "inputs": {}},
                "7": {"class_type": "PreviewImage", "inputs": {"images": ["10", 0]}},
                "10": {
                    "class_type": "KSampler",
                    "inputs": {
                        "model": ["1", 0],
                        "positive": ["2", 0],
                        "negative": ["3", 0],
                        "latent_image": ["4", 0],
                        "seed": 1,
                    },
                },
            }
        ),
        content_sha256="a" * 64,
        positive_node_id="2",
        negative_node_id="3",
        output_node_id="7",
        seed_node_id="10",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("urls", [(), ("https://gchat.qpic.cn/a", "https://gchat.qpic.cn/b")])
async def test_i2i_requires_exactly_one_image_before_upload(urls):
    definition = i2i_definition()
    client = SimpleNamespace(upload_input_image=AsyncMock())
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    with pytest.raises(WorkflowExecutionError, match="一张"):
        await service.prepare_request(
            workflow_key=definition.workflow_key, positive_prompt="改蓝色", negative_prompt="",
            image_urls=urls, request_scope="a" * 32,
        )
    client.upload_input_image.assert_not_called()


@pytest.mark.asyncio
async def test_i2i_prepare_deduplicates_upload_and_pins_workflow():
    definition = i2i_definition()
    uploaded = uploaded_image()
    client = SimpleNamespace(upload_input_image=AsyncMock(return_value=uploaded))
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    request = await service.prepare_request(
        workflow_key=definition.workflow_key, positive_prompt="改蓝色", negative_prompt="",
        image_urls=("https://gchat.qpic.cn/a",) * 2, request_scope="a" * 32,
    )
    assert request.input_image == uploaded
    assert request.workflow_sha256 == definition.execution_sha256
    client.upload_input_image.assert_awaited_once_with(
        "https://gchat.qpic.cn/a", request_scope="a" * 32,
    )
    assert (await service.describe_workflow(definition.workflow_key)).requires_image


@pytest.mark.asyncio
async def test_t2i_prepare_never_uploads_context_images():
    definition = _text_definition()
    client = SimpleNamespace(upload_input_image=AsyncMock(side_effect=AssertionError))
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    request = await service.prepare_request(
        workflow_key=definition.workflow_key, positive_prompt="cat", negative_prompt="",
        image_urls=("https://gchat.qpic.cn/a",), request_scope="a" * 32,
    )
    assert request.input_image is None
    assert request.workflow_sha256 is None
    client.upload_input_image.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["missing_image", "changed_hash", "changed_type"])
async def test_i2i_generate_rejects_missing_or_changed_preparation(change):
    definition = i2i_definition()
    if change == "changed_type":
        definition = replace(definition, workflow_type="text_to_image")
    client = SimpleNamespace(generate=AsyncMock())
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    with pytest.raises(WorkflowExecutionError):
        await service.generate(
            workflow_key=definition.workflow_key, positive_prompt="cat", negative_prompt="",
            input_image=None if change == "missing_image" else uploaded_image(),
            workflow_sha256="d" * 64 if change == "changed_hash" else "c" * 64,
        )
    client.generate.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["binding", "api", "fixed_positive", "fixed_negative"])
async def test_i2i_snapshot_rejects_execution_changes_without_ui_hash_change(change):
    """API 或管理员绑定独立变化时，排队原图不能被另一执行模板消费。"""
    definition = i2i_definition()
    api = json.loads(definition.api_json)
    api["18"] = {"class_type": "PrimitiveStringMultiline", "inputs": {"value": "preserve"}}
    api["5"]["inputs"]["string_b"] = ["18", 0]
    definition = replace(definition, api_json=json.dumps(api))
    original_hash = definition.content_sha256
    client = SimpleNamespace(upload_input_image=AsyncMock(return_value=uploaded_image()),
                             generate=AsyncMock())
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client,
    )
    request = await service.prepare_request(
        workflow_key=definition.workflow_key, positive_prompt="改蓝色", negative_prompt="",
        image_urls=("https://gchat.qpic.cn/source",), request_scope="a" * 32,
    )
    if change == "binding":
        definition = replace(definition, positive_node_id="18")
    elif change == "api":
        api["14"]["inputs"]["steps"] = 8
        definition = replace(definition, api_json=json.dumps(api))
    elif change == "fixed_positive":
        definition = replace(definition, additional_positive_prompt="red")
    else:
        definition = replace(definition, additional_negative_prompt="blue")
    assert definition.content_sha256 == original_hash
    with pytest.raises(WorkflowExecutionError) as error:
        await service.generate(
            workflow_key=request.workflow_key, positive_prompt=request.positive_prompt,
            negative_prompt=request.negative_prompt, input_image=request.input_image,
            workflow_sha256=request.workflow_sha256,
        )
    assert error.value.code == "WORKFLOW_CHANGED"
    client.generate.assert_not_called()


@pytest.mark.asyncio
async def test_list_workflows_exposes_description_without_fixed_prompts() -> None:
    """Agent 可读取用途描述，但固定追加提示词不应进入工具选择上下文。"""

    definition = WorkflowDefinition(
        workflow_key="portrait",
        display_name="Portrait",
        source_file="portrait.json",
        source_format="api",
        ui_json=None,
        api_json='{"1":{"class_type":"PreviewImage","inputs":{}}}',
        content_sha256="a" * 64,
        positive_node_id="2",
        negative_node_id="3",
        output_node_id="1",
        seed_node_id="4",
        description="适合写实人像，不适合动漫风格",
        additional_positive_prompt="PRIVATE_FIXED_POSITIVE",
        additional_negative_prompt="PRIVATE_FIXED_NEGATIVE",
    )

    class Store:
        """返回带独立元数据的工作流定义。"""

        def list_executable(self, *, limit: int) -> tuple[WorkflowDefinition, ...]:
            assert limit == 20
            return (definition,)

    service = WorkflowExecutionService(store=Store(), client=object())

    summaries = await service.list_workflows()

    assert summaries == (
        WorkflowSummary(
            key="portrait",
            name="Portrait",
            description="适合写实人像，不适合动漫风格",
        ),
    )
    assert "PRIVATE_FIXED" not in repr(summaries)


@pytest.mark.asyncio
async def test_workflow_failure_logs_reproduction_metadata_without_prompts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """工作流调用失败时应留下可复现元数据，但不得写入提示词内容。"""

    definition = replace(
        _text_definition(),
        workflow_key="workflow-98926948bfe1",
        display_name="Test workflow",
        source_file="test.json",
    )

    class Store:
        """返回固定工作流定义的测试仓库。"""

        def get_executable(self, workflow_key: str) -> WorkflowDefinition:
            assert workflow_key == "workflow-98926948bfe1"
            return definition

    class Client:
        """模拟 Comfy 在接收完整工作流后失败。"""

        async def generate(self, workflow, *, output_node_id: str):
            assert output_node_id == "7"
            assert workflow["2"]["inputs"]["text"] == "PRIVATE_POSITIVE_SENTINEL"
            raise WorkflowExecutionError("EXECUTION_FAILED", "PRIVATE_UPSTREAM_DETAIL")

    caplog.set_level(
        logging.WARNING,
        logger="agent_bot.services.workflow_execution",
    )
    service = WorkflowExecutionService(store=Store(), client=Client())

    with pytest.raises(WorkflowExecutionError):
        await service.generate(
            workflow_key="workflow-98926948bfe1",
            positive_prompt="PRIVATE_POSITIVE_SENTINEL",
            negative_prompt="PRIVATE_NEGATIVE_SENTINEL",
        )

    diagnostic = next(
        record.getMessage()
        for record in caplog.records
        if record.name == "agent_bot.services.workflow_execution"
    )
    assert "stage=comfy_generate" in diagnostic
    assert "workflow_key=workflow-98926948bfe1" in diagnostic
    assert f"workflow_sha256={'a' * 64}" in diagnostic
    assert "output_node_id=7" in diagnostic
    assert "seed=" in diagnostic
    assert "positive_chars=25" in diagnostic
    assert "negative_chars=25" in diagnostic
    assert "code=EXECUTION_FAILED" in diagnostic
    assert "PRIVATE_POSITIVE_SENTINEL" not in diagnostic
    assert "PRIVATE_NEGATIVE_SENTINEL" not in diagnostic
    assert "PRIVATE_UPSTREAM_DETAIL" not in diagnostic
