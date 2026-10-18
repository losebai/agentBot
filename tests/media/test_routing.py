"""本地媒体和识图入口保持独立的预设、权限与模型能力检查。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_bot.ai.routing import RoutedModelGuard, RoutingAiClient
from agent_bot.models import LlmRequestOptions
from media.test_generation_protocols import media_request
from media.test_inputs import picture
from media.test_validation import response


async def test_video_without_current_image_uses_chat_model():
    """视频只作为文字上下文，不能单独调用视觉模型。"""
    client, agent, _ = client_for()
    await client.complete(
        model="chat", session_id="s", prompt="[视频] 请看文字说明", messages=(), request_id="r",
        video_urls=("https://qq.com/video.mp4",), use_agent=True, use_web_search=False,
    )
    request = agent.complete.await_args.args[0]
    assert request.image_urls == request.image_material_ids == ()
    assert request.agent_code == "preset"


def client_for():
    """只替换服务端口，保留生产请求构造与模型能力判断。"""
    agent, media = AsyncMock(), AsyncMock()
    agent.catalog.get_system_prompt.return_value = "当前 Agent 人设"
    agent.complete.return_value = SimpleNamespace(content="看到了猫")
    media.generate.return_value = SimpleNamespace(response=response(picture()))
    client = RoutingAiClient(
        AsyncMock(), media_service=media, agent_service=agent, agent_code="preset",
        timeout_seconds=30, default_options=LlmRequestOptions(), mcp_services=(),
    )
    return client, agent, media


async def test_vision_only_has_no_tools_or_preset():
    """纯识图请求强制关闭工具与预设。"""
    client, agent, _ = client_for()
    assert (
        await client.complete(
            model="vision",
            session_id="s",
            prompt="看图",
            messages=(),
            request_id="r",
            image_urls=("https://qpic.cn/a",),
            use_agent=False,
            use_web_search=False,
        )
        == "看到了猫"
    )
    request, authority = agent.complete.await_args.args
    assert request.model == "vision" and request.agent_code is None
    assert request.tools == request.mcp_services == authority.allowed_tools == ()
    assert request.image_urls == ("https://qpic.cn/a",)


async def test_python_media_generation_and_guard_do_not_touch_java(media_model):
    """媒体生成与模型预检使用同一本地目录。"""
    client, _, media = client_for()
    request = media_request().model_copy(update={"model": media_model[2].alias})
    result = await client.generate_media(**request.model_dump(exclude={"request_timeout_millis"}))
    assert result.items[0].mime_type == "image/png"
    assert media.generate.await_args.args[1].entry == "qq"
    guard = RoutedModelGuard(media_model[0])
    await guard.ensure_enabled(request.model, capability=client.media_route("IMAGE").capability)
