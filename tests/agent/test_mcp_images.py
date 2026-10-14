"""截图 MCP 的大帧、图片校验、模型输入和错误边界回归。"""

import base64
import json
import random
from io import BytesIO

import httpx
import pytest
from mcp import types
from PIL import Image

from agent_bot.agent.contracts import ToolCall, ToolResult, TurnMessage
from agent_bot.agent.errors import AgentError
from agent_bot.agent.mcp.client import McpConnector
from agent_bot.agent.mcp.credentials import McpCredentials
from agent_bot.agent.mcp.discovery import convert_result
from agent_bot.ai.providers.langchain_messages import to_langchain_messages

from .mcp_server import peer
from .test_mcp import server


def screenshot_result():
    """构造超过旧 256 KiB 单帧限制的确定性 PNG，不读取真实桌面。"""
    output = BytesIO()
    with Image.frombytes("RGB", (400, 300), random.Random(0).randbytes(400 * 300 * 3)) as image:
        image.save(output, format="PNG")
    return {
        "content": [
            {"type": "text", "text": "截图观察结果"},
            {"type": "image", "mimeType": "image/png",
             "data": base64.b64encode(output.getvalue()).decode("ascii")},
        ], "isError": False,
    }


@pytest.mark.parametrize("response_sse", [True, False])
async def test_screenshot_response_reaches_model_without_exposing_image_bytes(response_sse):
    """真实 HTTP/SSE 截图结果保留图像，模型经 LangChain 收图，序列化不含二进制。"""
    payload = screenshot_result()
    async with peer(tool_result=payload, response_sse=response_sse) as (url, state):
        async with McpConnector(McpCredentials()).open(
            (server(url),), remaining=lambda: 10,
        ) as conn:
            result = await conn.call(ToolCall(call_id="shot-1", name="lookup", arguments={}))
    assert result.content == "截图观察结果" and len(result.images) == 1
    assert result.images[0].source_size_bytes > 262144
    assert "images" not in result.model_dump()
    assert payload["content"][1]["data"] not in result.model_dump_json()
    messages = to_langchain_messages((TurnMessage(role="tool", tool_result=result),))
    assert messages[0].type == "tool" and messages[0].tool_call_id == "shot-1"
    assert messages[1].type == "human"
    assert messages[1].content[1]["image_url"]["url"] == result.images[0].data_url()
    assert "shot-1" in messages[1].content[0]["text"]
    assert len(state["tool_requests"]) == 1


@pytest.mark.parametrize("response_sse", [True, False])
async def test_oversized_tool_response_keeps_its_precise_error(monkeypatch, response_sse):
    """工具响应超限必须报告大小错误，不能被 SDK 的流结束异常覆盖。"""
    from agent_bot.agent.mcp import http

    monkeypatch.setattr(http, "MAX_MCP_RESPONSE_BYTES", 1024)
    payload = {"content": [{"type": "text", "text": "x" * 2048}], "isError": False}
    async with peer(tool_result=payload, response_sse=response_sse) as (url, _):
        with pytest.raises(AgentError, match="AGENT_MCP_RESPONSE_TOO_LARGE"):
            async with McpConnector(McpCredentials()).open(
                (server(url),), remaining=lambda: 10,
            ) as conn:
                await conn.call(ToolCall(call_id="large", name="lookup", arguments={}))


@pytest.mark.parametrize("data,mime", [("invalid base64!", "image/png"), ("YWJj", "image/png")])
def test_invalid_mcp_image_is_rejected_without_logging_payload(data, mime):
    """仅接收通过真实图片解码的内容，底层错误不携带服务端数据。"""
    result = types.CallToolResult(content=[types.ImageContent(data=data, mime_type=mime)])
    with pytest.raises(AgentError):
        convert_result(ToolCall(call_id="bad", name="Screenshot", arguments={}), result)


def test_image_observations_follow_all_tool_replies_in_the_same_batch():
    """同批多工具回复不得被图片用户消息插断，否则模型 API 会拒绝调用顺序。"""
    result = convert_result(
        ToolCall(call_id="shot-1", name="Screenshot", arguments={}),
        types.CallToolResult.model_validate(screenshot_result()),
    )
    messages = to_langchain_messages((
        TurnMessage(role="tool", tool_result=result),
        TurnMessage(role="tool", tool_result=ToolResult(call_id="two", name="App", content="ok")),
    ))
    assert [message.type for message in messages] == ["tool", "tool", "human"]


@pytest.mark.parametrize("protocol", ["openai-chat", "openai-responses", "ollama"])
async def test_mcp_images_are_encoded_by_langchain_for_each_supported_protocol(protocol):
    """用离线模型传输检查三种协议均收到图片，不能仅在工具对象上保留图片。"""
    from agent.test_model_protocols import PROVIDERS, body, model_call
    from agent_bot.ai.providers.transport import ProviderTransport

    result = convert_result(
        ToolCall(call_id="shot-1", name="Screenshot", arguments={}),
        types.CallToolResult.model_validate(screenshot_result()),
    )
    sent = []

    def handle(request):
        """只捕获本地请求体，返回受控结果，不访问实际模型。"""
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body(protocol, tool=False))

    provider = PROVIDERS[protocol](ProviderTransport(httpx.MockTransport(handle)))
    call = model_call(protocol).model_copy(update={"messages": (
        TurnMessage(role="assistant", tool_calls=(
            ToolCall(call_id="shot-1", name="Screenshot", arguments={}),
        )),
        TurnMessage(role="tool", tool_result=result),
    )})
    try:
        await provider.complete_turn(call)
        image_data = base64.b64encode(result.images[0].data).decode("ascii")
        assert image_data in json.dumps(sent[0])
        assert "shot-1" in json.dumps(sent[0])
    finally:
        await provider.aclose()
