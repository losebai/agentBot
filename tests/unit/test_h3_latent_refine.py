"""H3 双阶段潜空间细化的授权边界与提示词同步。"""

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from agent_bot.services.workflow_inputs import WorkflowInputImage
from agent_bot.services.workflows import (
    WorkflowCatalogImporter,
    WorkflowValidationError,
    prepare_workflow_prompt,
)


def refine_api():
    """构造与真实服务隔离的双阶段工作流。"""
    api = json.loads(Path("src/resources/minimax_h3_r2v_upscale2x_api.json").read_text("utf-8"))
    for key in ("20", "21"):
        api.pop(key, None)
    api["28"] = {"class_type": "LTXVSeparateAVLatent", "inputs": {"av_latent": ["11", 0]}}
    api["30"] = {"class_type": "MinimaxH3LatentUpscaler3D", "inputs": {
        "latent": ["28", 0], "mode": "scale by multiplier", "mode.scale": 2.0,
    }}
    api["29"] = {"class_type": "LTXVConcatAVLatent", "inputs": {
        "video_latent": ["30", 0], "audio_latent": ["28", 1],
    }}
    api["31"] = copy.deepcopy(api["6"])
    for field in ("width", "height"):
        api["31"]["inputs"][field] = api["6"]["inputs"][field] * 2
    api["32"] = copy.deepcopy(api["17"])
    api["32"]["inputs"].update(positive=["31", 0], latent=["29", 0])
    api["33"] = copy.deepcopy(api["7"])
    api["33"]["inputs"]["conditioning"] = ["32", 0]
    api["34"] = copy.deepcopy(api["10"])
    api["34"]["inputs"].update(steps=6, denoise=0.25)
    api["35"] = copy.deepcopy(api["11"])
    api["35"]["inputs"].update(guider=["33", 0], sigmas=["34", 0], latent_image=["29", 0])
    api["12"]["inputs"]["samples"] = ["35", 0]
    api["14"]["inputs"]["images"] = ["12", 0]
    return api


def test_refine_preserves_primary_bindings_and_synchronizes_prompt():
    """同一上传图和种子供两段采样使用，最终提示词写入两个条件。"""
    api = refine_api()
    definition = WorkflowCatalogImporter().parse_json("refine.json", json.dumps(api))
    definition = replace(definition, additional_positive_prompt="保持身份")
    assert (definition.positive_node_id, definition.input_image_node_id,
            definition.seed_node_id, definition.output_node_id) == ("6", "1", "8", "15")
    image = WorkflowInputImage(filename="a" * 64 + ".png", subfolder="chat-i2i/" + "b" * 32,
                               media_type="image/png", size_bytes=10, sha256="a" * 64)
    prompt = prepare_workflow_prompt(definition, positive_prompt="镜头推近", negative_prompt="",
                                     seed=42, input_image=image)
    assert (prompt["6"]["inputs"]["prompt"] == prompt["31"]["inputs"]["prompt"]
            == "镜头推近, 保持身份")
    assert prompt["8"]["inputs"]["noise_seed"] == 42
    assert prompt["1"]["inputs"]["image"] == image.comfy_path
    assert prompt["13"]["inputs"]["samples"] == ["11", 0]
    assert json.loads(definition.api_json)["6"]["inputs"]["prompt"] == api["6"]["inputs"]["prompt"]


def test_refine_infers_roles_without_fixed_node_ids():
    """管理员导入重新编号的同一拓扑仍绑定主条件。"""
    api = refine_api()
    mapping = {key: str(int(key) + 100) for key in api}
    renamed = {}
    for key, node in api.items():
        for value in node["inputs"].values():
            if isinstance(value, list):
                value[0] = mapping[value[0]]
        renamed[mapping[key]] = node
    definition = WorkflowCatalogImporter().parse_json("renamed.json", json.dumps(renamed))
    assert definition.positive_node_id == "106"


@pytest.mark.parametrize("node,field,value", [
    ("31", "prompt", "stale prompt"),
    ("31", "width", 448),
    ("31", "length", 96),
    ("31", "clip", ["4", 0]),
    ("31", "vae", ["5", 0]),
    ("31", "ref_images.ref_image_0", ["12", 0]),
    ("31", "ref_images.ref_image_1", ["1", 0]),
    ("32", "latent", ["31", 1]),
    ("32", "image", ["12", 0]),
    ("32", "vae", ["5", 0]),
    ("32", "frame_idx", 1),
    ("30", "latent", ["6", 1]),
    ("30", "mode.scale", 4),
    ("29", "audio_latent", ["28", 0]),
    ("28", "av_latent", ["6", 1]),
    ("35", "noise", ["8", 1]),
    ("35", "latent_image", ["31", 1]),
    ("33", "model", ["3", 0]),
    ("34", "model", ["3", 0]),
    ("34", "denoise", 1),
    ("12", "samples", ["11", 0]),
    ("13", "samples", ["35", 0]),
])
def test_refine_rejects_invalid_stage_connections(node, field, value):
    """拒绝尺寸、身份条件、潜变量或原始音轨被替换。"""
    api = refine_api()
    api[node]["inputs"][field] = value
    with pytest.raises(WorkflowValidationError):
        WorkflowCatalogImporter().parse_json("invalid.json", json.dumps(api))


@pytest.mark.parametrize("extra", ["6", "8", "11", "1"])
def test_refine_rejects_hidden_additional_roles(extra):
    """隐藏未连接分支不得绕过授权图和唯一种子约束。"""
    api = refine_api()
    api["99"] = copy.deepcopy(api[extra])
    with pytest.raises(WorkflowValidationError):
        WorkflowCatalogImporter().parse_json("hidden.json", json.dumps(api))


@pytest.mark.parametrize("node", ["6", "31", "17", "32"])
@pytest.mark.parametrize("field", [
    "ref_videos.ref_video_0", "ref_video_audios.ref_video_audio_0", "ref_audios.ref_audio_0",
    "audio", "first_frame", "last_frame",
])
def test_refine_rejects_additional_reference_fields_on_both_stages(node, field):
    """即使额外素材处于输出可达链中，也不得被编码器或 Guide 隐式消费。"""
    api = refine_api()
    api["99"] = {"class_type": "EmptyImage", "inputs": {
        "width": 448, "height": 800, "batch_size": 48, "color": 0,
    }}
    # 同时添加到两个条件，防止单纯的阶段一致性比较掩盖额外参考输入。
    targets = ("6", "31") if node in {"6", "31"} else (node,)
    for target in targets:
        api[target]["inputs"][field] = ["99", 0]
    with pytest.raises(WorkflowValidationError):
        WorkflowCatalogImporter().parse_json("extra-reference.json", json.dumps(api))


def test_refine_rejects_alignment_that_changes_effective_canvas():
    """放大节点的对齐会改变真实输出，必须与第二阶段条件画布严格一致。"""
    api = refine_api()
    api["30"]["inputs"]["align"] = 512
    with pytest.raises(WorkflowValidationError):
        WorkflowCatalogImporter().parse_json("wrong-alignment.json", json.dumps(api))
