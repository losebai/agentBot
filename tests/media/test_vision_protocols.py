"""三种协议在工具往返后保持同一张用户图片，不公开图像字节。"""

import base64
import json

import httpx
import pytest
from pydantic import ValidationError

from agent.test_model_protocols import PROVIDERS, body, model_call
from agent_bot.agent.contracts import ToolResult, TurnMessage
from agent_bot.ai.providers.transport import ProviderTransport
from agent_bot.media.inputs import ImagePreparer
from media.test_inputs import picture


@pytest.mark.parametrize("protocol", PROVIDERS)
async def test_vision_tool_roundtrip(protocol):
    """请求图片只属于首条用户消息，第二轮不会丢失或复制到工具消息。"""
    sent = []

    def handle(request):
        """保留离线协议结构，用真实解析器完成两轮交互。"""
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body(protocol, len(sent) == 1))

    provider = PROVIDERS[protocol](ProviderTransport(httpx.MockTransport(handle)))
    image = ImagePreparer().prepare(picture(), None).model_image
    message = TurnMessage(role="user", text="图中有什么", images=(image,))
    request = model_call(protocol).model_copy(update={"messages": (message,)})
    encoded = base64.b64encode(image.data).decode()
    try:
        turn = await provider.complete_turn(request)
        followup = request.model_copy(
            update={
                "messages": (
                    *request.messages,
                    TurnMessage(
                        role="assistant",
                        text=turn.content,
                        tool_calls=turn.calls,
                        continuation=turn.continuation,
                    ),
                    TurnMessage(
                        role="tool",
                        tool_result=ToolResult(
                            call_id=turn.calls[0].call_id, name="web_search", content="facts"
                        ),
                    ),
                )
            }
        )
        final = await provider.complete_turn(followup)
        assert final.content == "已查询到结果"
        for payload in sent:
            serialized = json.dumps(payload)
            assert serialized.count(encoded) == 1
            if protocol == "ollama":
                assert payload["messages"][0]["images"] == [encoded]
            else:
                content = payload["input" if protocol == "openai-responses" else "messages"][0][
                    "content"
                ]
                assert content[0]["type"] == (
                    "input_text" if protocol == "openai-responses" else "text"
                )
                assert content[1]["type"] == (
                    "input_image" if protocol == "openai-responses" else "image_url"
                )
        assert encoded not in request.model_dump_json()
    finally:
        await provider.aclose()


def test_only_user_messages_can_have_images():
    """角色边界阻止工具结果或系统指令伪装成图像输入。"""
    image = ImagePreparer().prepare(picture(), None).model_image
    with pytest.raises(ValidationError):
        TurnMessage(role="assistant", images=(image,))
