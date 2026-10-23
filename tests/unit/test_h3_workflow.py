"""H3 独立视频契约与旧图片工作流之间的边界。"""

import copy
import json
from pathlib import Path

import pytest

from agent_bot.services.workflow_inputs import WorkflowInputImage
from agent_bot.services.workflows import (
    WorkflowCatalogImporter,
    WorkflowValidationError,
    prepare_workflow_prompt,
    validate_workflow_bindings,
)


def h3_api():
    """采用发布模板验证真实节点与动态编码字段。"""
    return json.loads(
        Path("src/resources/minimax_h3_r2v_upscale2x_api.json").read_text(encoding="utf-8")
    )


def test_h3_allows_explicit_long_budget_without_changing_defaults():
    """本机 H3 耗时超过旧上限，长预算只能由管理员明确配置。"""
    from agent_bot.config import RuntimeConfig

    base = dict(onebot_v11_access_token="x" * 32, chat_session_secret="y" * 32)
    assert RuntimeConfig(**base).chat_local_workflow_timeout_seconds == 500
    configured = RuntimeConfig(**base, chat_local_workflow_timeout_seconds=1800)
    assert configured.chat_local_workflow_timeout_seconds == 1800


def test_admin_explicit_long_timeout_reaches_agent_request():
    """管理入口不再将管理员明确配置的长预算裁到 300 秒。"""
    from types import SimpleNamespace

    from agent_bot.admin.agent_chat import AgentChatProbe, AgentProbeRequest

    probe = AgentChatProbe(SimpleNamespace(config=SimpleNamespace(chat_timeout_seconds=1800)))
    request = probe.request(
        AgentProbeRequest(agent_code="h3", prompt="制作视频", session_id="video"), "req"
    )
    assert request.timeout_seconds == 1800


def test_h3_import_and_bind_actual_prompt_noise_and_image():
    """没有负向节点的视频仍可执行，超分和原始音轨保留。"""
    definition = WorkflowCatalogImporter().parse_json("h3-api.json", json.dumps(h3_api()))
    assert definition.workflow_type == "image_to_video"
    assert definition.executable
    assert definition.negative_node_id is None
    validate_workflow_bindings(definition)
    image = WorkflowInputImage(
        filename="a" * 64 + ".png",
        subfolder="chat-i2i/" + "b" * 32,
        media_type="image/png",
        size_bytes=100,
        sha256="a" * 64,
    )
    prompt = prepare_workflow_prompt(
        definition, positive_prompt="镜头缓慢推近", negative_prompt="", seed=42, input_image=image
    )
    assert prompt["6"]["inputs"]["prompt"] == "镜头缓慢推近"
    assert prompt["8"]["inputs"]["noise_seed"] == 42
    assert prompt["1"]["inputs"]["image"] == image.comfy_path
    assert prompt["14"]["inputs"]["images"] == ["21", 0]
    assert prompt["14"]["inputs"]["audio"] == ["13", 0]
    assert "seed" not in prompt["11"]["inputs"]


def test_current_flat_canvas_matches_portable_api():
    """同步时读取真实平铺画布，转换出的执行输入与发布 API 一致。"""
    raw = Path("workflows/minimax-h3/minimax_h3_r2v_upscale2x.json").read_text("utf-8")
    definition = WorkflowCatalogImporter().parse_json("h3.json", raw)
    assert definition.executable and not definition.enabled
    expected = {
        key: {"class_type": node["class_type"], "inputs": node["inputs"]}
        for key, node in h3_api().items()
    }
    assert json.loads(definition.api_json) == expected


def test_ultrasharp720p_template_fuses_before_upscale_and_preserves_audio():
    """原始帧先可选降噪与 RAFT 融合，再超分至 720p 并轻微锐化，保留原音轨。"""
    api_path = Path("src/resources/minimax_h3_r2v_ultrasharp720p_api.json")
    api = json.loads(api_path.read_text("utf-8"))
    definition = WorkflowCatalogImporter().parse_json("h3-ultrasharp.json", json.dumps(api))
    assert definition.workflow_type == "image_to_video" and definition.executable
    assert api["20"]["inputs"]["model_name"] == "4x-UltraSharp.safetensors"
    assert api["21"]["class_type"] == "H3FrameUpscaleToSize"
    assert (api["21"]["inputs"]["width"], api["21"]["inputs"]["height"]) == (720, 1280)
    assert api["21"]["inputs"]["image"] == ["23", 0]
    assert api["21"]["inputs"]["sharpen_strength"] == 0.0
    assert api["21"]["inputs"]["enhancement_strength"] == 0.35
    assert api["22"]["class_type"] == "H3RAFTSmallLoader"
    assert api["22"]["inputs"]["model_name"] == "raft_small_C_T_V2-01064c6d.pth"
    assert api["23"]["class_type"] == "H3TemporalConsistency"
    assert api["23"]["inputs"]["images"] == ["24", 0]
    assert api["23"]["inputs"]["optical_flow"] == ["22", 0]
    assert api["23"]["inputs"]["flow_scale"] == 1.0
    assert api["23"]["inputs"]["luma_strength"] == 0.25
    assert api["23"]["inputs"]["chroma_strength"] == 0.35
    assert api["24"]["class_type"] == "H3FrameDenoise"
    assert api["24"]["inputs"] == {"images": ["12", 0], "strength": 0.0}
    assert api["14"]["inputs"]["images"] == ["21", 0]
    assert api["14"]["inputs"]["audio"] == ["13", 0]
    assert api["6"]["inputs"]["length"] == 192
    ui_path = Path("workflows/minimax-h3/minimax_h3_r2v_ultrasharp720p.json")
    ui = WorkflowCatalogImporter().parse_json("h3-ultrasharp.json", ui_path.read_text("utf-8"))
    assert json.loads(ui.api_json) == {
        key: {"class_type": value["class_type"], "inputs": value["inputs"]}
        for key, value in api.items()
    }


@pytest.mark.parametrize(
    "mutation",
    [
        "second_output",
        "second_image",
        "disconnected",
        "bad_codec",
        "bad_conditioning",
        "bad_latent",
        "static_output",
    ],
)
def test_h3_rejects_ambiguous_or_unsupported_bindings(mutation):
    """绑定不唯一、未连接或格式错误不得被当成可执行视频。"""
    api = copy.deepcopy(h3_api())
    if mutation == "second_output":
        api["99"] = copy.deepcopy(api["15"])
    elif mutation == "second_image":
        api["99"] = copy.deepcopy(api["1"])
    elif mutation == "disconnected":
        api["6"]["inputs"].pop("ref_images.ref_image_0")
    elif mutation == "bad_conditioning":
        api["7"]["inputs"]["conditioning"] = ["1", 0]
    elif mutation == "bad_latent":
        api["11"]["inputs"]["latent_image"] = ["1", 0]
    elif mutation == "static_output":
        api["14"]["inputs"]["images"] = ["1", 0]
    else:
        api["15"]["inputs"]["format"] = "webm"
    with pytest.raises(WorkflowValidationError):
        WorkflowCatalogImporter().parse_json("h3.json", json.dumps(api))


@pytest.mark.asyncio
async def test_video_preparation_snapshot_and_wrong_tool_never_submit():
    """保存视频类型与原图快照；类型不匹配、版本变化均在生成前失败。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent_bot.services.workflow_execution import (
        WorkflowExecutionError,
        WorkflowExecutionService,
    )

    definition = WorkflowCatalogImporter().parse_json("h3.json", json.dumps(h3_api()))
    image = WorkflowInputImage(
        filename="a" * 64 + ".png",
        subfolder="chat-i2i/" + "b" * 32,
        media_type="image/png",
        size_bytes=100,
        sha256="a" * 64,
    )
    client = SimpleNamespace(
        upload_input_image=AsyncMock(return_value=image),
        generate=AsyncMock(),
        generate_video=AsyncMock(),
    )
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client
    )
    prepared = await service.prepare_request(
        workflow_key="h3",
        positive_prompt="推镜",
        negative_prompt="",
        image_urls=("https://example.invalid/original.png",),
        request_scope="b" * 32,
    )
    assert prepared.output_kind == "video"
    assert prepared.workflow_sha256 == definition.execution_sha256
    with pytest.raises(WorkflowExecutionError, match="类型一致"):
        await service.generate(
            workflow_key="h3",
            positive_prompt="推镜",
            negative_prompt="",
            input_image=image,
            workflow_sha256=prepared.workflow_sha256,
        )
    with pytest.raises(WorkflowExecutionError, match="已更新"):
        await service.generate_video(
            workflow_key="h3",
            positive_prompt="推镜",
            negative_prompt="",
            input_image=image,
            workflow_sha256="0" * 64,
        )
    client.generate.assert_not_awaited()
    client.generate_video.assert_not_awaited()


@pytest.mark.asyncio
async def test_video_tool_queues_once_and_recovers_persistent_receipt():
    """真实工具注册、准备与 Redis 回执共同防止重复上传和生成。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    import fakeredis.aioredis

    from agent_bot.integrations.tool_operations import RedisToolOperationStore
    from agent_bot.services.chat_tools import ChatToolContext, build_chat_tool_registry
    from agent_bot.services.workflow_execution import (
        WorkflowExecutionService,
        WorkflowImageCollector,
    )

    definition = WorkflowCatalogImporter().parse_json("h3.json", json.dumps(h3_api()))
    image = WorkflowInputImage(
        filename="a" * 64 + ".png",
        subfolder="chat-i2i/" + "b" * 32,
        media_type="image/png",
        size_bytes=100,
        sha256="a" * 64,
    )
    client = SimpleNamespace(
        upload_input_image=AsyncMock(return_value=image), generate_video=AsyncMock()
    )
    service = WorkflowExecutionService(
        store=SimpleNamespace(get_executable=lambda _: definition), client=client
    )
    redis = fakeredis.aioredis.FakeRedis()
    operations = RedisToolOperationStore(redis, key_prefix="h3-tool", ttl_seconds=60)
    submitted = AsyncMock(return_value="image-" + "b" * 32)
    try:
        for _ in range(2):
            collector = WorkflowImageCollector()
            collector.submit = submitted
            context = ChatToolContext(
                session_id="qq:test",
                user_id="10000",
                job_id="parent",
                allowed_tools=frozenset({"local_workflow_generate_video"}),
                workflow_service=service,
                workflow_collector=collector,
                image_urls=("https://example.invalid/source.png",),
                workflow_request_scope="b" * 32,
                operation_store=operations,
            )
            result = await build_chat_tool_registry().call(
                "local_workflow_generate_video",
                {
                    "workflow_key": "h3",
                    "positive_prompt": "推镜",
                    "start_reply": "开始制作",
                },
                context,
            )
            assert not result.is_error, result.content
            assert collector.queued_task_id == "image-" + "b" * 32
        submitted.assert_awaited_once()
        assert submitted.await_args.args[0].output_kind == "video"
        client.upload_input_image.assert_awaited_once()
        client.generate_video.assert_not_awaited()
    finally:
        await redis.aclose()
