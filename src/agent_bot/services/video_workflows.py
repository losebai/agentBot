"""H3 单图视频及受控双阶段细化的显式绑定校验。"""

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


def _validate_refinement(api: dict, encoders: list[str], samplers: list[str]) -> tuple[str, str]:
    """按拓扑识别两段采样，限定视频放大、原音轨及高分辨率身份条件。"""
    def unique(kind: str) -> str:
        """细化协议中每个处理角色只能出现一次。"""
        matches = [key for key, node in api.items() if node["class_type"] == kind]
        if len(matches) != 1:
            raise WorkflowValidationError("H3 refinement roles are ambiguous")
        return matches[0]

    def require(node: str, field: str, value: object) -> None:
        """关键连线必须精确匹配源节点和输出槽位。"""
        if api[node]["inputs"].get(field) != value:
            raise WorkflowValidationError(f"H3 refinement {field} binding is invalid")

    if len(encoders) != 2 or len(samplers) != 2:
        raise WorkflowValidationError("H3 requires one or two controlled sampling stages")
    primary_pairs = [(sampler, encoder) for sampler in samplers for encoder in encoders
                     if api[sampler]["inputs"].get("latent_image") == [encoder, 1]]
    if len(primary_pairs) != 1:
        raise WorkflowValidationError("H3 primary sampling stage is ambiguous")
    primary_sampler, primary = primary_pairs[0]
    secondary = next(key for key in encoders if key != primary)
    final_sampler = next(key for key in samplers if key != primary_sampler)
    split = unique("LTXVSeparateAVLatent")
    upscale = unique("MinimaxH3LatentUpscaler3D")
    concat = unique("LTXVConcatAVLatent")
    require(split, "av_latent", [primary_sampler, 0])
    require(upscale, "latent", [split, 0])
    require(upscale, "mode", "scale by multiplier")
    require(upscale, "mode.scale", 2.0)
    require(concat, "video_latent", [upscale, 0])
    require(concat, "audio_latent", [split, 1])
    require(final_sampler, "latent_image", [concat, 0])
    original = api[primary]["inputs"]
    refined = api[secondary]["inputs"]
    if api[primary]["class_type"] != api[secondary]["class_type"]:
        raise WorkflowValidationError("H3 refinement encoder type differs")
    image_field = ("ref_images.ref_image_0"
                   if api[primary]["class_type"] == "MiniMaxH3ReferenceToVideo" else "first_frame")
    encoder_fields = {"prompt", "clip", "vae", "width", "height", "length", image_field}
    if api[primary]["class_type"] == "MiniMaxH3ReferenceToVideo":
        encoder_fields.add("ref_image_size")
    # 可达性不能证明素材已获授权；只开放当前单图协议使用的字段，拒绝视频、音频及尾帧。
    if any(set(api[key]["inputs"]) - encoder_fields for key in encoders):
        raise WorkflowValidationError("H3 refinement contains unsupported reference inputs")
    # 第二个条件必须基于同一授权图和编码器，帧数保持不变，画布显式放大两倍。
    for field in ("prompt", "clip", "vae", "length", "ref_image_size", "first_frame",
                  "ref_images.ref_image_0"):
        if original.get(field) != refined.get(field):
            raise WorkflowValidationError("H3 refinement conditioning differs")
    for field in ("width", "height"):
        dimension = original.get(field)
        if type(dimension) is not int or dimension <= 0 or refined.get(field) != dimension * 2:
            raise WorkflowValidationError("H3 refinement dimensions must be twice the source")
        align = api[upscale]["inputs"].get("align", 32)
        if type(align) is not int or align <= 0:
            raise WorkflowValidationError("H3 refinement alignment is invalid")
        # 配套节点先按 align 对齐目标像素，再按 VAE 的 16 像素格对齐，条件必须匹配实际画布。
        effective_dimension = round(round(dimension * 2 / align) * align / 16) * 16
        if effective_dimension != refined[field]:
            raise WorkflowValidationError("H3 refinement alignment changes the target canvas")
    image = unique("LoadImage")
    noise = unique("RandomNoise")
    guider_ids = []
    for sampler, encoder, latent in (
        (primary_sampler, primary, [primary, 1]),
        (final_sampler, secondary, [concat, 0]),
    ):
        require(sampler, "noise", [noise, 0])
        guider_link = api[sampler]["inputs"].get("guider")
        if not _is_api_link(guider_link) or guider_link[1] != 0:
            raise WorkflowValidationError("H3 refinement guider is invalid")
        guider = guider_link[0]
        if api[guider]["class_type"] != "BasicGuider":
            raise WorkflowValidationError("H3 refinement requires BasicGuider")
        guider_ids.append(guider)
        require(guider, "model", api[guider_ids[0]]["inputs"].get("model"))
        conditioning = api[guider]["inputs"].get("conditioning")
        if not _is_api_link(conditioning) or conditioning[1] != 0:
            raise WorkflowValidationError("H3 refinement requires an image guide")
        guide = conditioning[0]
        if api[guide]["class_type"] != "MiniMaxH3AddGuide":
            raise WorkflowValidationError("H3 refinement requires an image guide")
        if set(api[guide]["inputs"]) - {"positive", "latent", "frame_idx", "vae", "image"}:
            raise WorkflowValidationError("H3 refinement guide has unsupported reference inputs")
        for field, value in (("positive", [encoder, 0]), ("latent", latent),
                             ("image", [image, 0]), ("vae", original.get("vae")),
                             ("frame_idx", 0)):
            require(guide, field, value)
    if len(set(guider_ids)) != 2:
        raise WorkflowValidationError("H3 refinement guiders must be separate")
    if len([n for n in api.values() if n["class_type"] == "BasicGuider"]) != 2:
        raise WorkflowValidationError("H3 refinement contains an extra guider")
    require(final_sampler, "sampler", api[primary_sampler]["inputs"].get("sampler"))
    scheduler_link = api[final_sampler]["inputs"].get("sigmas")
    if not _is_api_link(scheduler_link) or scheduler_link[1] != 0:
        raise WorkflowValidationError("H3 refinement scheduler is invalid")
    scheduler = scheduler_link[0]
    schedule = api[scheduler]["inputs"]
    if (api[scheduler]["class_type"] != "BasicScheduler"
            or type(schedule.get("steps")) is not int or schedule["steps"] <= 0
            or type(schedule.get("denoise")) not in (int, float)
            or not 0 < schedule["denoise"] < 1):
        raise WorkflowValidationError("H3 refinement requires a low denoise scheduler")
    require(scheduler, "model", api[guider_ids[0]]["inputs"].get("model"))
    video = unique("VAEDecode")
    audio = unique("VAEDecodeAudio")
    compose = unique("CreateVideo")
    require(video, "samples", [final_sampler, 0])
    require(video, "vae", original.get("vae"))
    require(audio, "samples", [primary_sampler, 0])
    require(compose, "images", [video, 0])
    require(compose, "audio", [audio, 0])
    # 严格拓扑不允许隐藏的编码器、图片或额外种子在未使用分支中执行。
    output = unique("SaveVideo")
    if _api_upstream_node_ids(api, output) | {output} != set(api):
        raise WorkflowValidationError("H3 refinement contains disconnected nodes")
    for key, node in api.items():
        if key != noise and {"seed", "noise_seed"} & node["inputs"].keys():
            raise WorkflowValidationError("H3 refinement requires a unique noise source")
    return primary, primary_sampler


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

    encoders = [key for key, node in api.items() if node["class_type"] in H3_TYPES]
    samplers = [key for key, node in api.items()
                if node["class_type"] == "SamplerCustomAdvanced"]
    refinement = len(encoders) != 1 or len(samplers) != 1
    if refinement:
        positive, sampler = _validate_refinement(api, encoders, samplers)
    else:
        positive, sampler = encoders[0], samplers[0]
    image = unique({"LoadImage"})
    noise = unique({"RandomNoise"})
    output = unique({"SaveVideo"})
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
    guider = api[sampler]["inputs"]["guider"][0] if refinement else unique({"BasicGuider"})
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
