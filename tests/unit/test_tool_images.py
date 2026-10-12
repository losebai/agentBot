import base64
import json

import httpx
import pytest

from agent_bot.integrations.dispatch_ai import DispatchAiClient
from agent_bot.models import DispatchAiSession
from agent_bot.services.tool_images import capture_tool_images

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nimage").decode()


def test_session_preserves_validated_tool_images():
    """图片附件必须经过协议模型保留，旧文本响应仍兼容。"""
    result = DispatchAiSession.model_validate({
        "replayContext": "截图", "images": [{"mimeType": "image/png", "data": PNG}],
    })
    assert result.images[0].data == PNG
    assert DispatchAiSession(replayContext="旧回复").images == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["截图完成", ""])
async def test_client_captures_images_without_turning_them_into_text(reply):
    """图片与文本独立返回，纯图片也可完成请求。"""
    async def handler(request):
        """提供标准 AI 响应，不访问网络。"""
        return httpx.Response(200, json={"code": 200, "data": {
            "replayContext": reply, "images": [{"mimeType": "image/png", "data": PNG}],
        }})

    client = DispatchAiClient(base_url="http://ai.test/dispatch-ai", timeout_seconds=3,
                              transport=httpx.MockTransport(handler))
    try:
        with capture_tool_images() as images:
            answer = await client.complete(model="test", session_id="s", prompt="截图",
                                           messages=(), request_id="req", user="tester",
                                           chat_tools=("memory",), tool_session_token="x" * 40)
            assert answer == reply
            assert images.items[0].data == PNG
        with capture_tool_images() as other:
            assert other.items == ()
    finally:
        await client.aclose()



@pytest.mark.asyncio
@pytest.mark.parametrize("final_type", ["done", "error", None])
async def test_stream_images_are_published_only_after_success(final_type):
    """流未完成或报错时不能泄漏已经收到的图片给下一次请求。"""
    from agent_bot.integrations.dispatch_ai import (
        DispatchAiProtocolError,
        DispatchAiUnavailableError,
    )

    async def handler(request):
        """模拟独立图片事件及终态。"""
        events = [{"choices": [{"index": 0, "delta": {"type": "images", "images": [
            {"mimeType": "image/png", "data": PNG},
        ]}}]}]
        if final_type:
            events.append({"choices": [{"index": 0, "delta": {"type": final_type}}]})
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"},
                              text="".join("data: " + json.dumps(e) + "\n\n" for e in events))

    client = DispatchAiClient(base_url="http://ai.test/dispatch-ai", timeout_seconds=3,
                              transport=httpx.MockTransport(handler))
    try:
        with capture_tool_images() as images:
            if final_type == "done":
                assert await client.complete(model="test", session_id="s", prompt="截图",
                                             messages=(), request_id="req") == ""
                assert images.items[0].data == PNG
            else:
                with pytest.raises((DispatchAiProtocolError, DispatchAiUnavailableError)):
                    await client.complete(model="test", session_id="s", prompt="截图",
                                          messages=(), request_id="req")
                assert images.items == ()
    finally:
        await client.aclose()


@pytest.mark.parametrize("mime,data", [("image/jpeg", PNG), ("image/png", "invalid"),
                                       ("image/svg+xml", PNG)])
def test_response_rejects_invalid_images(mime, data):
    """拒绝无法验证的 MIME 和图片数据。"""
    with pytest.raises(ValueError):
        DispatchAiSession.model_validate({"replayContext": "", "images": [
            {"mimeType": mime, "data": data},
        ]})


def test_collection_enforces_total_size_and_deduplicates():
    """多个合法附件仍需受总大小限制，重复图片不多次发送。"""
    from agent_bot.media_artifacts import ImageArtifact
    from agent_bot.services.tool_images import ToolImageCollection

    collection = ToolImageCollection()
    image = ImageArtifact(mimeType="image/png", data=PNG)
    collection.add((image, image))
    assert collection.items == (image,)
    large = ImageArtifact(mimeType="image/png", data=base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"x" * 10_485_752,
    ).decode())
    with pytest.raises(ValueError):
        collection.add((large,))
    assert collection.items == (image,)
