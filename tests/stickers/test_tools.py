"""表情工具按真实会话交付原件，防止伪造ID和重复刷屏。"""

import json
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority, ToolCall
from agent_bot.agent.errors import AgentError
from agent_bot.services.tool_images import capture_tool_images

from .test_service import Library, sticker


def tool_session(*, entry="qq", enabled=True):
    """只替换模型服务和外部令牌解析，不给模型可控身份参数。"""
    from agent_bot.plugins.stickers.tools import StickerTools

    item = sticker(enabled=enabled)
    service = SimpleNamespace(store=Library([item]), search=AsyncMock(return_value={"matches": [
        {"sticker": item.model_dump(mode="json"), "score": 0.9, "reason": "符合语境"},
    ]}))
    authority = ExecutionAuthority(entry=entry, allowed_tools=("sticker_search", "sticker_send"))
    request = AgentRequest(prompt="今天好累", session_id="s", request_id="r")
    context = SimpleNamespace(session_id="s", allowed_tools=frozenset(authority.allowed_tools),
                              ensure_active=lambda: None)
    bridge = SimpleNamespace(context=AsyncMock(return_value=context))
    adapter = StickerTools(lambda: service)
    return adapter.bind(request, authority, None, None, bridge), service, context


async def invoke(session, name, arguments):
    """使用真实工具调用契约，确保参数经过同一授权执行路径。"""
    return await session.call(ToolCall(call_id=name, name=name, arguments=arguments))


async def test_cannot_send_an_id_before_context_search():
    """表情ID不能凭空指定，必须来自本请求的检索结果。"""
    session, _, _ = tool_session()
    with pytest.raises(AgentError) as rejected:
        await invoke(session, "sticker_send", {"id": "a" * 32})
    assert rejected.value.code == "STICKER_NOT_SELECTED"


async def test_search_then_send_registers_original_gif_once():
    """检索返回元数据，发送才交付原件，重复调用不能在本轮刷屏。"""
    session, service, _ = tool_session()
    first, second = Image.new("RGB", (2, 2), "red"), Image.new("RGB", (2, 2), "blue")
    buffer = BytesIO()
    first.save(buffer, format="GIF", save_all=True, append_images=[second], duration=50)
    original = buffer.getvalue()
    service.store.read = AsyncMock(return_value=(sticker(), original))
    result = await invoke(session, "sticker_search", {"context": "今天累得瘫下来了"})
    assert json.loads(result.content)["matches"][0]["sticker"]["id"] == "a" * 32
    assert not result.images and not result.attachments
    with capture_tool_images() as captured:
        await invoke(session, "sticker_send", {"id": "a" * 32})
        await invoke(session, "sticker_send", {"id": "a" * 32})
    import base64

    assert len(captured.items) == 1
    assert base64.b64decode(captured.items[0].data) == original
    assert captured.items[0].mime_type == "image/gif"


async def test_disabled_after_search_is_not_sent():
    """检索后管理员停用的表情不能沿用旧结果投递。"""
    session, service, _ = tool_session()
    await invoke(session, "sticker_search", {"context": "加班"})
    service.store.rows[0] = sticker(enabled=False)
    with capture_tool_images() as captured:
        with pytest.raises(AgentError) as rejected:
            await invoke(session, "sticker_send", {"id": "a" * 32})
    assert rejected.value.code == "STICKER_UNAVAILABLE" and not captured.items


async def test_qq_context_must_match_request_and_tool_whitelist():
    """不能把别人的有效令牌上下文用于当前会话，也不能越过原始授权。"""
    session, service, context = tool_session()
    context.session_id = "someone-else"
    with pytest.raises(AgentError) as rejected:
        await invoke(session, "sticker_search", {"context": "加班"})
    assert rejected.value.code == "AGENT_TOOL_UNAUTHORIZED"
    service.search.assert_not_awaited()


async def test_plugin_shutdown_revokes_bound_tool_calls():
    """工具快照不能复活已停用的插件。"""
    from agent_bot.plugins.stickers.tools import StickerTools

    request = AgentRequest(prompt="你好", session_id="s", request_id="r")
    session = StickerTools(lambda: None).bind(
        request, ExecutionAuthority(entry="admin", allowed_tools=("sticker_search",)),
        None, None, None,
    )
    with pytest.raises(AgentError) as rejected:
        await invoke(session, "sticker_search", {"context": "加班"})
    assert rejected.value.code == "STICKER_DISABLED"
