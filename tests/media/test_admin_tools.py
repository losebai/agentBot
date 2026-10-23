"""管理媒体工具不借用 QQ 身份，所有结果保存为受控附件。"""

import asyncio
import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from agent_bot.agent.contracts import ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.integrations.comfy import ComfyClient
from agent_bot.integrations.gpt_sovits import GeneratedSpeech
from agent_bot.media.inputs import ImagePreparer
from agent_bot.services.image_task_context import active_image_task
from agent_bot.services.workflow_execution import GeneratedWorkflowImage, WorkflowImageRequest
from media.test_inputs import picture
from media.test_store import SCOPE, storage


def workflow_call(key="portrait"):
    """使用原始严格参数模型要求的全部字段。"""
    return ToolCall(
        call_id="1",
        name="local_workflow_generate_image",
        arguments={"workflow_key": key, "start_reply": "开始绘图", "positive_prompt": "一只猫"},
    )


def adapters(store, tmp_path):
    """只替换上游端口，配额、校验和文件保存仍使用生产实现。"""
    from agent_bot.agent.tools.media import AdminMediaTools

    workflow = SimpleNamespace(
        list_workflows=AsyncMock(return_value=()),
        describe_workflow=AsyncMock(return_value=SimpleNamespace(requires_image=False)),
        prepare_request=AsyncMock(
            return_value=WorkflowImageRequest(workflow_key="portrait", positive_prompt="cat")
        ),
        generate=AsyncMock(
            return_value=GeneratedWorkflowImage(media_type="image/png", data=picture())
        ),
    )
    wav = tmp_path / "speech.wav"
    wav.write_bytes(b"RIFF\x04\x00\x00\x00WAVE")
    tts = SimpleNamespace(generate=AsyncMock(return_value=GeneratedSpeech(wav, "audio/wav", 12)))
    comfy = SimpleNamespace(cancel=AsyncMock())
    config = SimpleNamespace(
        chat_local_workflow_enabled=True, chat_tts_enabled=True
    )
    return AdminMediaTools(config, workflow, tts, store, comfy_client=comfy), workflow, tts, comfy


async def test_admin_generates_one_image_and_one_speech(
    media_database, agent_params, tmp_path, media_clock
):
    """每种生成额度最多一次，重复调用不产生第二次上游副作用。"""
    async with storage(media_database, agent_params, tmp_path / "media", media_clock) as store:
        tools, workflow, tts, _ = adapters(store, tmp_path)
        session = tools.bind(SCOPE, ())
        assert {tool.name for tool in session.definitions} == {
            "local_workflow_list",
            "local_workflow_generate_image",
            "local_workflow_generate_video",
            "tts_generate",
        }
        image = await session.call(workflow_call())
        speech = await session.call(
            ToolCall(call_id="2", name="tts_generate", arguments={"text": "\uff08微笑\uff09你好😊"})
        )
        assert image.attachments[0].status == speech.attachments[0].status == "ready"
        assert "path" not in speech.content and "storage_key" not in image.content
        assert tts.generate.await_args.args == ("你好",)
        for call in [
            workflow_call(),
            ToolCall(call_id="3", name="tts_generate", arguments={"text": "再次"}),
        ]:
            with pytest.raises(AgentError, match="AGENT_TOOL_LIMIT"):
                await session.call(call)
        assert workflow.generate.await_count == tts.generate.await_count == 1
        assert (tmp_path / "speech.wav").exists()


async def test_admin_rejects_invalid_args_and_image_count(
    media_database, agent_params, tmp_path, media_clock
):
    """工作流键、任意路径、QQ 专属工具和多图在副作用之前失败。"""
    async with storage(media_database, agent_params, tmp_path / "media", media_clock) as store:
        tools, workflow, _, _ = adapters(store, tmp_path)
        session = tools.bind(SCOPE, ())
        for call in [
            workflow_call("../secret"),
            ToolCall(call_id="x", name="local_workflow_tasks", arguments={}),
            ToolCall(
                call_id="x", name="tts_generate", arguments={"text": "hello", "path": "C:/private"}
            ),
        ]:
            with pytest.raises(AgentError):
                await session.call(call)
        workflow.generate.assert_not_called()
        workflow.describe_workflow.return_value = SimpleNamespace(requires_image=True)
        image = ImagePreparer().prepare(picture(), None)
        for images in [(), (image, image)]:
            with pytest.raises(AgentError, match="MEDIA_IMAGE_REQUIRED"):
                await tools.bind(SCOPE, images).call(workflow_call())
        workflow.prepare_request.assert_not_called()


async def test_admin_cancel_only_owns_current_prompt(
    media_database, agent_params, tmp_path, media_clock
):
    """请求取消只撤销本次捕获的 prompt_id，不调用全局中断。"""
    async with storage(media_database, agent_params, tmp_path / "media", media_clock) as store:
        tools, workflow, _, comfy = adapters(store, tmp_path)
        ready = asyncio.Event()

        async def generate(**kwargs):
            """模拟真实 Comfy 在提交后记录提示标识。"""
            await active_image_task.get().submitted("owned-prompt", "http://comfy.invalid")
            ready.set()
            await asyncio.Event().wait()

        workflow.generate.side_effect = generate
        task = asyncio.create_task(tools.bind(SCOPE, ()).call(workflow_call()))
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        comfy.cancel.assert_awaited_once_with("owned-prompt")


async def test_comfy_internal_bytes_upload_uses_server_scope():
    """内部上传不允许远端 URL 或任意 Comfy 路径，原字节保持不变。"""
    data = picture()
    filename = hashlib.sha256(data).hexdigest() + ".png"

    def handle(request):
        """只允许一次 multipart 上传，不提交图片生成。"""
        assert request.method == "POST" and request.url.path == "/upload/image"
        assert data in request.content
        return httpx.Response(
            200,
            json={
                "name": filename,
                "subfolder": f"chat-i2i/{SCOPE.execution_id}",
                "type": "input",
            },
        )

    client = ComfyClient(
        base_url="http://comfy.invalid", timeout_seconds=30, transport=httpx.MockTransport(handle)
    )
    try:
        uploaded = await client.upload_input_bytes(
            data, media_type="image/png", request_scope=SCOPE.execution_id
        )
        assert uploaded.sha256 == hashlib.sha256(data).hexdigest()
    finally:
        await client.aclose()
