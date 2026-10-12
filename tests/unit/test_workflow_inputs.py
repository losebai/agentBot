"""图生图缺图拒绝、请求隔离和文生图不变性的回归。"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_bot.services.workflows import (
    WorkflowDefinition,
    WorkflowValidationError,
    _load_sdxl_api_template,
    prepare_workflow_prompt,
)


def i2i_definition():
    """提供完整连线的离线 Qwen 编辑图。"""
    return WorkflowDefinition(
        workflow_key="qwen-i2i", display_name="I2I", source_file="i2i.json",
        source_format="ui", ui_json=None,
        api_json=(Path(__file__).parents[1] / "fixtures/qwen_i2i_api.json").read_text(
            encoding="utf-8",
        ), content_sha256="c" * 64, positive_node_id="2", negative_node_id="12",
        output_node_id="16", seed_node_id="14", workflow_type="image_to_image",
        input_image_node_id="1", positive_input_name="value",
        negative_input_name="negative_prompt",
    )


def uploaded_image(**overrides):
    """构造网关已上传原图的受限引用，不含 URL 或图片字节。"""
    from agent_bot.services.workflow_inputs import WorkflowInputImage

    return WorkflowInputImage(**{
        "filename": "b" * 64 + ".png", "subfolder": "dispatch-chat-i2i/" + "a" * 32,
        "media_type": "image/png", "size_bytes": 100, "sha256": "b" * 64,
        **overrides,
    })


def test_i2i_missing_image_never_uses_sample():
    definition = i2i_definition()
    with pytest.raises(WorkflowValidationError, match="image"):
        prepare_workflow_prompt(definition, positive_prompt="改蓝色", negative_prompt="", seed=1)


@pytest.mark.parametrize("change", ["encoder_bypass", "extract_bypass", "instruction_bypass"])
def test_i2i_rejects_bypassed_prompt_enhancement_chain(change):
    """存在 PE 节点不代表执行链真的使用了它和本次编辑指令。"""
    from agent_bot.services.workflows import validate_workflow_bindings

    definition = i2i_definition()
    api = json.loads(definition.api_json)
    if change == "encoder_bypass":
        api["12"]["inputs"]["prompt"] = ["2", 0]
    elif change == "extract_bypass":
        api["7"]["inputs"]["json_string"] = ["2", 0]
    else:
        api["6"]["inputs"]["prompt"] = "fixed instruction"
        # 指令通过其他无关字符串字段进入输出上游，不算传入 PE。
        api["7"]["inputs"]["key"] = ["2", 0]
    with pytest.raises(WorkflowValidationError):
        validate_workflow_bindings(replace(definition, api_json=json.dumps(api)))


def test_i2i_replaces_only_bound_fields_and_keeps_template():
    definition = i2i_definition()
    original = json.loads(definition.api_json)
    expected = json.loads(definition.api_json)
    expected["1"]["inputs"]["image"] = "dispatch-chat-i2i/" + "a" * 32 + "/" + "b" * 64 + ".png"
    expected["2"]["inputs"]["value"] = "改蓝色"
    expected["12"]["inputs"]["negative_prompt"] = "模糊"
    expected["14"]["inputs"]["seed"] = 42
    actual = prepare_workflow_prompt(
        definition, positive_prompt="改蓝色", negative_prompt="模糊", seed=42,
        input_image=uploaded_image(),
    )
    assert actual == expected
    assert json.loads(definition.api_json) == original


@pytest.mark.parametrize("changes", [
    {"positive_input_name": "ckpt_name"}, {"positive_node_id": "9"},
    {"input_image_node_id": "9"}, {"input_image_node_id": None},
    {"negative_input_name": "prompt"}, {"output_node_id": "8"},
])
def test_i2i_rejects_unsafe_or_incomplete_bindings(changes):
    definition = replace(i2i_definition(), **changes)
    with pytest.raises(WorkflowValidationError):
        prepare_workflow_prompt(
            definition, positive_prompt="改蓝色", negative_prompt="", seed=1,
            input_image=uploaded_image(),
        )


@pytest.mark.parametrize("mutation", ["extra_image", "disconnected", "cycle"])
def test_i2i_rejects_ambiguous_image_graph(mutation):
    definition = i2i_definition()
    api = json.loads(definition.api_json)
    if mutation == "extra_image":
        api["18"] = {"class_type": "LoadImage", "inputs": {"image": "private.png"}}
        api["12"]["inputs"]["images.image_2"] = ["18", 0]
    elif mutation == "disconnected":
        api["12"]["inputs"].pop("images.image_1")
    else:
        api["2"]["inputs"]["value"] = ["7", 0]
    with pytest.raises(WorkflowValidationError):
        prepare_workflow_prompt(
            replace(definition, api_json=json.dumps(api)), positive_prompt="改色",
            negative_prompt="", seed=1, input_image=uploaded_image(),
        )


@pytest.mark.parametrize("changes", [
    {"filename": "../other.png"}, {"subfolder": "../../private"},
    {"subfolder": "C:/private"}, {"subfolder": "dispatch-chat-i2i/../x"},
    {"filename": "b" * 64 + ".jpg"}, {"sha256": "c" * 64},
    {"size_bytes": 0}, {"size_bytes": 10_485_761}, {"media_type": "image/gif"},
])
def test_input_reference_rejects_invalid_path_content_or_size(changes):
    with pytest.raises(ValidationError):
        uploaded_image(**changes)


def test_legacy_t2i_remains_identical_and_rejects_input_reference():
    definition = WorkflowDefinition(
        workflow_key="sdxl", display_name="SDXL", source_file="sdxl.json",
        source_format="api", ui_json=None, api_json=_load_sdxl_api_template(),
        content_sha256="a" * 64, positive_node_id="2", negative_node_id="3",
        output_node_id="7", seed_node_id="10",
    )
    expected = json.loads(definition.api_json)
    expected["2"]["inputs"]["text"] = "花"
    expected["3"]["inputs"]["text"] = "模糊"
    expected["10"]["inputs"]["seed"] = 42
    assert prepare_workflow_prompt(
        definition, positive_prompt="花", negative_prompt="模糊", seed=42,
    ) == expected
    with pytest.raises(WorkflowValidationError):
        prepare_workflow_prompt(
            definition, positive_prompt="花", negative_prompt="", seed=42,
            input_image=uploaded_image(),
        )


def write_i2i_pair(directory):
    """从独立 API fixture 构造端口完整的 UI/API 配对。"""
    definition = i2i_definition()
    api = json.loads(definition.api_json)
    nodes, links = [], []
    for key, node in api.items():
        inputs = []
        widgets = []
        for name, value in node["inputs"].items():
            port = {"name": name, "type": "*", "link": None}
            if isinstance(value, list):
                link_id = len(links) + 1
                port["link"] = link_id
                links.append([link_id, int(value[0]), value[1], int(key), len(inputs), "*"])
            else:
                port["widget"] = {"name": name}
                widgets.append(value)
            inputs.append(port)
        nodes.append({"id": int(key), "type": node["class_type"], "inputs": inputs,
                      "outputs": [{"type": "*"}] * 3, "widgets_values": widgets})
    ui = {"nodes": nodes, "links": links}
    (directory / "edit.json").write_text(json.dumps(ui), encoding="utf-8")
    (directory / "api").mkdir()
    (directory / "api/edit-api.json").write_text(definition.api_json, encoding="utf-8")
    binding_fields = (
        "workflow_type", "positive_node_id", "negative_node_id", "output_node_id",
        "seed_node_id", "input_image_node_id", "positive_input_name", "negative_input_name",
    )
    (directory / "edit.meta.json").write_text(json.dumps({
        "description": "Do not import", "additional_positive_prompt": "Do not import",
        "workflow_bindings": {name: getattr(definition, name) for name in binding_fields},
    }), encoding="utf-8")
    return api


def test_importer_keeps_exact_explicit_i2i_api_and_ignores_admin_metadata(tmp_path):
    from agent_bot.services.workflows import WorkflowCatalogImporter

    api = write_i2i_pair(tmp_path)
    (definition,) = WorkflowCatalogImporter().scan(tmp_path)
    assert json.loads(definition.api_json) == api
    assert definition.workflow_type == "image_to_image"
    assert definition.positive_input_name == "value"
    assert definition.enabled is False
    assert definition.description == ""
    assert definition.additional_positive_prompt == ""


@pytest.mark.parametrize("problem", ["missing", "malformed", "mismatch"])
def test_importer_never_falls_back_when_explicit_api_is_invalid(tmp_path, problem):
    from agent_bot.services.workflows import WorkflowCatalogImporter

    api = write_i2i_pair(tmp_path)
    path = tmp_path / "api/edit-api.json"
    if problem == "missing":
        path.unlink()
    elif problem == "malformed":
        path.write_text("invalid", encoding="utf-8")
    else:
        api["14"]["inputs"]["steps"] = 12
        path.write_text(json.dumps(api), encoding="utf-8")
    with pytest.raises(WorkflowValidationError):
        WorkflowCatalogImporter().scan(tmp_path)
