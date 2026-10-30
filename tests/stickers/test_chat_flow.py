"""通过真实执行图确认可选表情检索失败不会中断聊天正文。"""

import json
from types import SimpleNamespace

import pytest

from agent.test_graph import prepared
from agent_bot.agent.contracts import ModelEvent, ModelTurn, ToolCall
from agent_bot.agent.graph import build_agent_graph
from agent_bot.agent.tools.langchain import LangChainTools
from agent_bot.agent.tools.runtime import BoundTools
from agent_bot.ai.errors import AiError
from agent_bot.services.tool_images import capture_tool_images
from stickers.test_service import service, sticker
from stickers.test_tools import tool_session


@pytest.mark.parametrize("error_code", ["STICKER_SEARCH_INVALID", "AI_RESPONSE_INVALID"])
async def test_invalid_sticker_search_allows_chat_to_finish_without_image(error_code):
    """格式失败仍记录失败工具结果，随后产出正文和完成事件，不能冒充图片成功。"""
    session, _, _ = tool_session()
    current, complete, _ = service([sticker(tags=())])
    if error_code == "STICKER_SEARCH_INVALID":
        complete.return_value = SimpleNamespace(content="invalid-json")
    else:
        complete.side_effect = AiError(error_code)
    session.tools.service_provider = lambda: current

    class Provider:
        """只替代付费聊天模型；执行图、工具校验和表情检索均使用真实实现。"""

        async def stream_turn(self, call):
            """首轮检索，收到明确错误后继续正文，不补发虚构图片。"""
            results = [m.tool_result for m in call.messages if m.role == "tool"]
            if not results:
                yield ModelEvent(kind="turn_end", turn=ModelTurn(model="offline", calls=(
                    ToolCall(call_id="search", name="sticker_search", arguments={
                        "context": "加班累瘫了，想表达无奈",
                    }),
                )))
                return
            assert len(results) == 1 and results[0].is_error
            assert json.loads(results[0].content)["code"] == error_code
            yield ModelEvent(kind="text_delta", delta="辛苦啦，先歇一会儿。")
            yield ModelEvent(kind="turn_end", turn=ModelTurn(
                model="offline", content="辛苦啦，先歇一会儿。",
            ))

    run = prepared(5)
    run.authority = session.authority
    graph = build_agent_graph(Provider(), run, LangChainTools(BoundTools(
        session.definitions, {d.name: session.call for d in session.definitions},
    )))
    with capture_tool_images() as captured:
        events = [event async for event in graph.astream({}, stream_mode="custom")]
    assert events[-1].kind == "done"
    assert [event.delta for event in events if event.kind == "text_delta"] == [
        "辛苦啦，先歇一会儿。",
    ]
    assert not captured.items
    results = [event for event in events if event.kind == "tool_result"]
    assert len(results) == 1 and results[0].code == "AGENT_TOOL_FAILED"
