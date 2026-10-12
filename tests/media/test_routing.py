"""媒体后端和 Agent 后端的能力组合，不允许失败后回退。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.ai.routing import AiRouteRequest, RoutedModelGuard, RoutingAiClient, select_route
from agent_bot.models import LlmRequestOptions
from media.test_generation_protocols import media_request
from media.test_inputs import picture
from media.test_validation import response


@pytest.mark.parametrize(
    "agent_backend,media_backend",
    [("java", "java"), ("java", "python"), ("python", "java"), ("python", "python")],
)
def test_route_matrix(agent_backend, media_backend):
    """只有图片依赖媒体开关，混合能力同时依赖两个开关，未迁移能力整条走 Java。"""
    kwargs = dict(plain_backend="python", agent_backend=agent_backend, media_backend=media_backend)
    assert select_route(AiRouteRequest(), **kwargs).capability == "ordinary-text"
    assert select_route(AiRouteRequest(images=True), **kwargs).backend == media_backend
    for mixed in [
        AiRouteRequest(images=True, agent=True),
        AiRouteRequest(chat_tools=("tts_generate",)),
        AiRouteRequest(images=True, chat_tools=("memory",)),
        AiRouteRequest(images=True, web=True),
    ]:
        expected = "python" if agent_backend == media_backend == "python" else "java"
        assert select_route(mixed, **kwargs).backend == expected
    for legacy in [
        AiRouteRequest(images=True, videos=True),
        AiRouteRequest(chat_tools=("tts_generate", "job")),
        AiRouteRequest(mcp_services=("laya",), images=True),
    ]:
        assert select_route(legacy, **kwargs).backend == "java"


def client_for(*, agent_backend="java", media_backend="python"):
    """只替换模型服务端口，路由和请求构造仍使用生产实现。"""
    agent, media, java = AsyncMock(), AsyncMock(), AsyncMock()
    agent.complete.return_value = SimpleNamespace(content="看到了猫")
    media.generate.return_value = SimpleNamespace(response=response(picture()))
    client = RoutingAiClient(
        java,
        AsyncMock(),
        plain_backend="python",
        agent_backend=agent_backend,
        media_backend=media_backend,
        media_service=media,
        agent_service=agent,
        agent_code="preset",
        timeout_seconds=30,
        default_options=LlmRequestOptions(),
        mcp_services=(),
        laya_enabled=False,
    )
    return client, agent, media, java


async def test_vision_only_has_no_tools_or_preset():
    """Agent 开关为 Java 时纯识图仍可走 Python，且强制关闭工具与预设。"""
    client, agent, _, java = client_for()
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
    java.complete.assert_not_called()


async def test_python_media_generation_and_guard_do_not_touch_java(media_model):
    """媒体生成与模型预检选择同一 Python 目录。"""
    client, _, media, java = client_for()
    request = media_request().model_copy(update={"model": media_model[2].alias})
    result = await client.generate_media(**request.model_dump(exclude={"request_timeout_millis"}))
    assert result.items[0].mime_type == "image/png"
    java.generate_media.assert_not_called()
    assert media.generate.await_args.args[1].entry == "qq"
    guard = RoutedModelGuard(
        AsyncMock(), media_model[0], plain_backend="java", media_backend="python"
    )
    await guard.ensure_enabled(request.model, capability=client.media_route("IMAGE").capability)
    guard.java_guard.ensure_enabled.assert_not_called()
