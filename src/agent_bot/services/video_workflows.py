"""H3 单图视频的显式绑定校验，不复用图片工作流的负向提示假设。"""

import copy
import json

from agent_bot.services.workflows import (
    WorkflowValidationError,
    _api_upstream_node_ids,
    _is_api_link,
    _UiWorkflowConverter,
    _validate_acyclic_dependencies,
)

H3_TYPES = {"MiniMaxH3ReferenceToVideo", "MiniMaxH3ImageToVideo"}


def ui_to_video_api(payload: dict) -> dict:
    """只转换 H3 平铺画布；常量取具名控件，连线仍按双向槽位严格校验。"""
    converter = _UiWorkflowConverter(payload)
    converter._links = converter._read_links(payload.get("links", []))
    result = {}
    for key, node in converter._nodes.items():
        if node["type"] in {"Note", "MarkdownNote"}:
            continue
        if node.get("mode", 0) != 0:
            raise WorkflowValidationError("H3 contains an inactive node")
        named = node.get("widgets_values_named")
        if named is None:
            result[key] = converter._convert_node(key)
            continue
        if not isinstance(named, dict):
            raise WorkflowValidationError("H3 named widget values are invalid")
        inputs = copy.deepcopy(named)
        for field in ("upload", "control_after_generate"):
            inputs.pop(field, None)
        for item in converter._node_inputs(node):
            if item.get("link") is not None:
                source, slot, _, _, _ = converter._linked_input(item["link"])
                inputs[item["name"]] = [source, slot]
        result[key] = {"class_type": node["type"], "inputs": inputs}
    return result


def infer_video_bindings(api: dict) -> tuple[str, str, str, str]:
    """唯一图像、文本条件、噪声和视频输出必须处于同一条生成链。"""
    _validate_acyclic_dependencies(api)

    def unique(types):
        """角色必须唯一，额外图片或输出不能藏在未使用分支。"""
        matches = [key for key, node in api.items() if node["class_type"] in types]
        if len(matches) != 1:
            raise WorkflowValidationError("H3 video bindings are ambiguous")
        return matches[0]

    positive = unique(H3_TYPES)
    image = unique({"LoadImage"})
    noise = unique({"RandomNoise"})
    output = unique({"SaveVideo"})
    sampler = unique({"SamplerCustomAdvanced"})
    inputs = api[positive]["inputs"]
    image_field = (
        "ref_images.ref_image_0"
        if api[positive]["class_type"] == "MiniMaxH3ReferenceToVideo"
        else "first_frame"
    )
    if inputs.get(image_field) != [image, 0] or not isinstance(inputs.get("prompt"), str):
        raise WorkflowValidationError("H3 input image or prompt is invalid")
    if any(name.startswith("ref_images.") and name != image_field for name in inputs):
        raise WorkflowValidationError("H3 requires exactly one reference image")
    if api[sampler]["inputs"].get("noise") != [noise, 0]:
        raise WorkflowValidationError("H3 noise binding is disconnected")
    if api[sampler]["inputs"].get("latent_image") != [positive, 1]:
        raise WorkflowValidationError("H3 latent binding is disconnected")
    guider = unique({"BasicGuider"})
    if api[sampler]["inputs"].get("guider") != [guider, 0]:
        raise WorkflowValidationError("H3 guider binding is disconnected")
    conditioning = api[guider]["inputs"].get("conditioning")
    # 首帧 Guide 可以串联，但每个 Guide 必须使用同一张授权图片和同一潜变量。
    while _is_api_link(conditioning):
        node = api[conditioning[0]]
        if node["class_type"] != "MiniMaxH3AddGuide":
            break
        guide = node["inputs"]
        if (conditioning[1] != 0 or guide.get("image") != [image, 0]
                or guide.get("latent") != [positive, 1]):
            raise WorkflowValidationError("H3 guide does not use the authorized input")
        conditioning = guide.get("positive")
    if conditioning != [positive, 0]:
        raise WorkflowValidationError("H3 prompt binding is disconnected")
    seed = api[noise]["inputs"].get("noise_seed")
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed < 2**64:
        raise WorkflowValidationError("H3 noise seed is invalid")
    reachable = _api_upstream_node_ids(api, output)
    if not {positive, image, noise, sampler} <= reachable:
        raise WorkflowValidationError("H3 bindings are disconnected")
    # 本次只交付 MP4/H.264，不能把其他容器当作 MP4 下载并发送。
    save = api[output]["inputs"]
    if save.get("format") != "mp4" or save.get("format.codec", save.get("codec")) != "h264":
        raise WorkflowValidationError("H3 requires MP4 H.264 output")
    compose = unique({"CreateVideo"})
    frames = api[compose]["inputs"].get("images")
    if (
        save.get("video") != [compose, 0]
        or not _is_api_link(frames)
        or sampler not in _api_upstream_node_ids(api, frames[0])
    ):
        raise WorkflowValidationError("H3 output does not contain sampled video frames")
    return positive, image, noise, output


def validate_video_bindings(definition) -> None:
    """数据库中的绑定必须与原图唯一输出角色完全一致。"""
    try:
        api = json.loads(definition.api_json or "")
        inferred = infer_video_bindings(api)
    except (ValueError, KeyError, TypeError) as error:
        raise WorkflowValidationError("H3 video workflow is invalid") from error
    configured = (
        definition.positive_node_id,
        definition.input_image_node_id,
        definition.seed_node_id,
        definition.output_node_id,
    )
    if (
        configured != inferred
        or definition.negative_node_id is not None
        or definition.positive_input_name != "prompt"
        or definition.additional_negative_prompt
    ):
        raise WorkflowValidationError("H3 video bindings are invalid")
