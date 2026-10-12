import base64
import json

import httpx
import pytest

from agent_bot.integrations.dispatch_ai import (
    DispatchAiClient,
    DispatchAiImageError,
    DispatchAiProtocolError,
    DispatchAiRejectedError,
    DispatchAiUnavailableError,
)
from agent_bot.models import ChatContext, ChatMessage, LlmRequestOptions

VALID_PNG_BASE64 = "iVBORw0KGgo="
VALID_MP3_BASE64 = "SUQz"


@pytest.mark.asyncio
async def test_generate_media_posts_typed_image_request_and_validates_base64() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url == "http://ai.test/dispatch-ai/chat/mediaOne"
        assert 1 <= payload.pop("requestTimeoutMillis") <= 3_000
        assert payload == {
            "requestId": "qq-media-1",
            "sessionId": "private:1",
            "user": "tester",
            "model": "image-model",
            "outputType": "IMAGE",
            "prompt": "画一只猫",
            "messages": [],
            "timeoutSeconds": 60,
            "maxBytes": 10_485_760,
            "imageFormat": "png",
        }
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "text": None,
                    "items": [
                        {
                            "type": "IMAGE",
                            "mimeType": "image/png",
                            "data": VALID_PNG_BASE64,
                        }
                    ],
                },
            },
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        response = await client.generate_media(
            model="image-model",
            session_id="private:1",
            prompt="画一只猫",
            messages=(),
            request_id="qq-media-1",
            output_type="IMAGE",
            timeout_seconds=60,
            max_bytes=10_485_760,
            image_format="png",
            voice=None,
            audio_format=None,
            user="tester",
        )
    finally:
        await client.aclose()

    assert response.items[0].data == VALID_PNG_BASE64


@pytest.mark.asyncio
async def test_generate_media_posts_native_audio_messages() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == "audio-model"
        assert payload["outputType"] == "AUDIO"
        assert payload["messages"] == [{"role": "user", "content": "读给我听"}]
        assert payload["timeoutSeconds"] == 21
        assert payload["voice"] == "alloy"
        assert payload["audioFormat"] == "mp3"
        assert "prompt" not in payload
        return httpx.Response(
            200,
            json={
                "code": 200,
                "data": {
                    "text": "已经读完",
                    "items": [
                        {
                            "type": "AUDIO",
                            "mimeType": "audio/mpeg",
                            "data": VALID_MP3_BASE64,
                        }
                    ],
                },
            },
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        response = await client.generate_media(
            model="audio-model",
            session_id="group:1",
            prompt=None,
            messages=(ChatMessage(role="user", content="读给我听"),),
            request_id="qq-media-2",
            output_type="AUDIO",
            timeout_seconds=21.9,
            max_bytes=5_242_880,
            image_format=None,
            voice="alloy",
            audio_format="mp3",
            user="tester",
        )
    finally:
        await client.aclose()

    assert response.text == "已经读完"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        {"type": "IMAGE", "mimeType": "image/png", "data": "not-base64"},
        {"type": "IMAGE", "mimeType": "image/jpeg", "data": VALID_PNG_BASE64},
        {
            "type": "IMAGE",
            "mimeType": "image/png",
            "data": f"data:image/png;base64,{VALID_PNG_BASE64}",
        },
        {
            "type": "IMAGE",
            "mimeType": "image/png",
            "data": VALID_PNG_BASE64,
            "url": "https://media.example.test/image.png",
        },
    ],
)
async def test_generate_media_rejects_invalid_or_url_responses(
    item: dict[str, str],
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": 200, "data": {"text": None, "items": [item]}},
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DispatchAiProtocolError):
            await client.generate_media(
                model="image-model",
                session_id="private:1",
                prompt="画图",
                messages=(),
                request_id="qq-media-invalid",
                output_type="IMAGE",
                timeout_seconds=60,
                max_bytes=10_485_760,
                image_format="png",
                voice=None,
                audio_format=None,
                user="tester",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_generate_media_preserves_stable_interface_error_code() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "code": 500,
                "description": "MEDIA_PROTOCOL_UNSUPPORTED",
                "data": None,
            },
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DispatchAiRejectedError) as exc_info:
            await client.generate_media(
                model="image-model",
                session_id="private:1",
                prompt="画图",
                messages=(),
                request_id="qq-media-unsupported",
                output_type="IMAGE",
                timeout_seconds=60,
                max_bytes=10_485_760,
                image_format="png",
                voice=None,
                audio_format=None,
                user="tester",
            )
    finally:
        await client.aclose()

    assert exc_info.value.status_code == 502
    assert exc_info.value.error_code == "MEDIA_PROTOCOL_UNSUPPORTED"


def _agent_chunk(
    event_type: str,
    content: str | None,
    *,
    model: str = "model",
) -> dict[str, object]:
    return {
        "id": "run-1",
        "object": "chat.completion.chunk",
        "created": 1_786_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "content": content,
                    "reasoningContent": None,
                    "thinking": False,
                    "type": event_type,
                    "toolCallId": None,
                    "toolName": None,
                    "toolSource": None,
                    "args": None,
                    "toolResult": None,
                },
                "finishReason": "stop",
            }
        ],
        "systemFingerprint": None,
        "usage": None,
    }


def _agent_result_response(content: str, *, model: str = "model") -> httpx.Response:
    chunk = _agent_chunk("result", content, model=model)
    done = _agent_chunk("done", None, model=model)
    return httpx.Response(
        200,
        headers={"Content-Type": "text/event-stream; charset=utf-8"},
        text=f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(done)}\n\n",
    )


@pytest.mark.asyncio
async def test_client_uses_streaming_agent_chat_when_web_search_is_enabled() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url == "http://ai.test/dispatch-ai/chat/agentChat"
        assert request.headers["authorization"] == "Bearer service-token"
        assert request.headers["user-agent"] == "dispatch-chat/qq_session"
        assert request.headers["x-request-id"] == "req-1"
        assert request.headers["accept"] == "text/event-stream"
        assert 1 <= payload.pop("requestTimeoutMillis") <= 3_000
        assert payload == {
            "prompt": "继续",
            "sessionId": "qq_session",
            "model": "Qwen/Qwen3-8B",
            "requestsId": "req-1",
            "tools": ["web_search"],
            "maxSteps": 3,
            "messages": [
                {"role": "system", "content": "请简洁回答"},
                {"role": "user", "content": "继续"},
            ],
        }
        return _agent_result_response("这是回复", model="Qwen/Qwen3-8B")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        token="service-token",
        transport=httpx.MockTransport(handler),
    )

    try:
        answer = await client.complete(
            model="Qwen/Qwen3-8B",
            session_id="qq_session",
            prompt="继续",
            messages=[
                ChatMessage(role="system", content="请简洁回答"),
                ChatMessage(role="user", content="继续"),
            ],
            request_id="req-1",
        )
    finally:
        await client.aclose()

    assert answer == "这是回复"


@pytest.mark.asyncio
async def test_client_uses_agent_chat_without_tools_when_web_search_is_disabled() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url == "http://ai.test/dispatch-ai/chat/agentChat"
        assert request.headers["accept"] == "text/event-stream"
        assert "tools" not in payload
        assert "maxSteps" not in payload
        return _agent_result_response("纯文本 Agent 回复")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        answer = await client.complete(
            model="model",
            session_id="s",
            prompt="看看 https://example.com/page",
            messages=[ChatMessage(role="user", content="看看 https://example.com/page")],
            request_id="r",
            use_agent=True,
            use_web_search=False,
        )
    finally:
        await client.aclose()

    assert answer == "纯文本 Agent 回复"


@pytest.mark.asyncio
async def test_client_maps_agent_error_event_without_exposing_content() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        chunk = _agent_chunk("error", "provider secret details")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n",
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(DispatchAiUnavailableError) as exc_info:
            await client.complete(
                model="model",
                session_id="s",
                prompt="p",
                messages=[ChatMessage(role="user", content="p")],
                request_id="r",
            )
    finally:
        await client.aclose()

    assert "provider secret details" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_client_rejects_oversized_agent_reply() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return _agent_result_response("x" * 1_000_001)

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(DispatchAiProtocolError, match="too large"):
            await client.complete(
                model="model",
                session_id="s",
                prompt="p",
                messages=[ChatMessage(role="user", content="p")],
                request_id="r",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_rejects_oversized_agent_event() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        action = _agent_chunk("action", "x" * 2_000_001)
        result = _agent_chunk("result", "最终回复")
        done = _agent_chunk("done", None)
        return httpx.Response(
            200,
            headers={"Content-Type": "text/event-stream"},
            text=(
                f"data: {json.dumps(action)}\n\n"
                f"data: {json.dumps(result)}\n\n"
                f"data: {json.dumps(done)}\n\n"
            ),
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(DispatchAiProtocolError, match="event is too large"):
            await client.complete(
                model="model",
                session_id="s",
                prompt="p",
                messages=[ChatMessage(role="user", content="p")],
                request_id="r",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_posts_typed_current_qq_chat_context() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url == "http://ai.test/dispatch-ai/chat/chatOne"
        assert payload["chatContext"] == {
            "platform": "qq",
            "scene": "group",
            "groupId": "20001",
            "groupName": "测试交流群",
            "senderId": "10001",
            "senderName": "测试成员",
        }
        return httpx.Response(
            200,
            json={"code": 200, "data": {"replayContext": "已识别当前群和发言人"}},
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    context = ChatContext(
        scene="group",
        groupId="20001",
        groupName="测试交流群",
        senderId="10001",
        senderName="测试成员",
    )

    try:
        answer = await client.complete(
            model="model",
            session_id="s",
            prompt="现在在哪里聊天？",
            messages=[ChatMessage(role="user", content="现在在哪里聊天？")],
            request_id="r",
            chat_context=context,
            use_agent=False,
            use_web_search=False,
        )
    finally:
        await client.aclose()

    assert answer == "已识别当前群和发言人"


@pytest.mark.asyncio
async def test_client_can_use_chat_one_without_web_search() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url == "http://ai.test/dispatch-ai/chat/chatOne"
        assert "tools" not in payload
        assert "maxSteps" not in payload
        return httpx.Response(
            200,
            json={"code": 200, "data": {"replayContext": "离线战绩点评"}},
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        answer = await client.complete(
            model="glm-5",
            session_id="naraka-ai",
            prompt="战绩 JSON",
            messages=[ChatMessage(role="user", content="战绩 JSON")],
            request_id="req-naraka",
            use_agent=False,
            use_web_search=False,
        )
    finally:
        await client.aclose()

    assert answer == "离线战绩点评"


@pytest.mark.asyncio
async def test_client_posts_configured_llm_options_and_current_user_name() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["maxTokens"] == 4096
        assert payload["temperature"] == 0.6
        assert payload["topP"] == 0.8
        assert payload["topK"] == 40
        assert payload["minP"] == 0.05
        assert payload["presencePenalty"] == 0.1
        assert payload["frequencyPenalty"] == 0.2
        assert payload["repetitionPenalty"] == 1.1
        assert payload["stop"] == ["END"]
        assert payload["enableThinking"] is True
        assert payload["user"] == "当前 QQ 昵称"
        return _agent_result_response("ok")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        request_options=LlmRequestOptions(
            max_tokens=4096,
            temperature=0.6,
            top_p=0.8,
            top_k=40,
            min_p=0.05,
            presence_penalty=0.1,
            frequency_penalty=0.2,
            repetition_penalty=1.1,
            stop=["END"],
            enable_thinking=True,
        ),
        transport=httpx.MockTransport(handler),
    )

    try:
        answer = await client.complete(
            model="model",
            session_id="s",
            prompt="p",
            messages=[ChatMessage(role="user", content="p")],
            request_id="r",
            user="当前 QQ 昵称",
        )
    finally:
        await client.aclose()

    assert answer == "ok"


@pytest.mark.asyncio
async def test_client_allows_bounded_options_for_internal_memory_compression() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path.endswith("/chat/chatOne")
        assert "maxSteps" not in payload
        assert payload["maxTokens"] == 256
        assert payload["temperature"] == 0.2
        assert payload["enableThinking"] is False
        assert "topP" not in payload
        return httpx.Response(
            200,
            json={"code": 200, "data": {"replayContext": "压缩摘要"}},
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        request_options=LlmRequestOptions(top_p=0.9, enable_thinking=True),
        agent_max_steps=5,
        transport=httpx.MockTransport(handler),
    )

    try:
        answer = await client.complete(
            model="model",
            session_id="s:memory",
            prompt="待压缩对话",
            messages=[ChatMessage(role="user", content="待压缩对话")],
            request_id="memory-r",
            use_agent=False,
            use_web_search=False,
            request_options=LlmRequestOptions(
                max_tokens=256,
                temperature=0.2,
                enable_thinking=False,
            ),
        )
    finally:
        await client.aclose()

    assert answer == "压缩摘要"


@pytest.mark.asyncio
async def test_client_posts_images_as_responses_input_content() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            if request.url.host == "gchat.qpic.cn":
                return httpx.Response(
                    200,
                    content=b"\xff\xd8\xfffirst-image",
                    headers={"Content-Type": "image/jpeg"},
                )
            if request.url.host == "multimedia.nt.qq.com.cn":
                return httpx.Response(
                    200,
                    content=b"\x89PNG\r\n\x1a\nsecond-image",
                    headers={"Content-Type": "image/png"},
                )
            raise AssertionError("unexpected image host")

        payload = json.loads(request.content)
        assert payload["model"] == "vision-model"
        assert "imageUrls" not in payload
        assert "messages" not in payload
        assert payload["input"] == [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "比较图片"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/jpeg;base64,"
                        + base64.b64encode(b"\xff\xd8\xfffirst-image").decode("ascii"),
                    },
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,"
                        + base64.b64encode(
                            b"\x89PNG\r\n\x1a\nsecond-image"
                        ).decode("ascii"),
                    },
                ],
            }
        ]
        assert "token=one" not in request.content.decode("utf-8")
        assert "token=two" not in request.content.decode("utf-8")
        return _agent_result_response("识别完成", model="vision-model")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        answer = await client.complete(
            model="vision-model",
            session_id="s",
            prompt="比较图片",
            messages=[ChatMessage(role="user", content="比较图片")],
            request_id="r",
            image_urls=(
                "https://gchat.qpic.cn/first.jpg?token=one",
                "https://multimedia.nt.qq.com.cn/second.png?token=two",
            ),
        )
    finally:
        await client.aclose()

    assert answer == "识别完成"


@pytest.mark.asyncio
async def test_client_posts_video_urls_without_downloading_them() -> None:
    video_url = "https://gzc-download.ftn.qq.com/quoted.mp4?token=one"

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        payload = json.loads(request.content)
        assert payload["videoUrls"] == [video_url]
        return _agent_result_response("视频识别完成", model="vision-model")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        answer = await client.complete(
            model="vision-model",
            session_id="s",
            prompt="分析视频",
            messages=[ChatMessage(role="user", content="分析视频")],
            request_id="r",
            video_urls=(video_url,),
        )
    finally:
        await client.aclose()

    assert answer == "视频识别完成"


@pytest.mark.asyncio
async def test_client_rejects_non_image_download_before_calling_dispatch_ai() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            content=b"<html>not an image</html>",
            headers={"Content-Type": "text/html"},
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(DispatchAiImageError):
            await client.complete(
                model="vision-model",
                session_id="s",
                prompt="识别图片",
                messages=[ChatMessage(role="user", content="识别图片")],
                request_id="r",
                image_urls=("https://gchat.qpic.cn/not-an-image",),
            )
    finally:
        await client.aclose()

    assert len(requests) == 1
    assert requests[0].method == "GET"


@pytest.mark.asyncio
async def test_client_rejects_untrusted_image_download_host() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("untrusted image URL must not be requested")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(DispatchAiImageError):
            await client.complete(
                model="vision-model",
                session_id="s",
                prompt="识别图片",
                messages=[ChatMessage(role="user", content="识别图片")],
                request_id="r",
                image_urls=("https://attacker.example/image.png",),
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_max_steps", "expected_steps", "enable_thinking"),
    [(None, 4, None), (1, 1, False), (5, 5, True)],
)
async def test_client_applies_configured_web_search_agent_step_limit(
    agent_max_steps: int | None, expected_steps: int, enable_thinking: bool | None,
) -> None:
    """普通聊天轮次优先，未设置时沿用旧联网配置。"""
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/chat/agentChat")
        payload = json.loads(request.content)
        assert payload["tools"] == ["web_search"]
        assert payload["maxSteps"] == expected_steps
        if enable_thinking is None:
            assert "enableThinking" not in payload
        else:
            assert payload["enableThinking"] is enable_thinking
        return _agent_result_response("带来源的搜索结果")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        web_search_max_steps=4,
        agent_max_steps=agent_max_steps,
        request_options=LlmRequestOptions(enable_thinking=enable_thinking),
        transport=httpx.MockTransport(handler),
    )

    try:
        answer = await client.complete(
            model="model",
            session_id="s",
            prompt="今天有什么新闻？",
            messages=[ChatMessage(role="user", content="今天有什么新闻？")],
            request_id="r",
        )
    finally:
        await client.aclose()

    assert answer == "带来源的搜索结果"


@pytest.mark.asyncio
async def test_client_maps_http_error_without_exposing_body() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream-secret-details")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(DispatchAiUnavailableError) as exc_info:
            await client.complete(
                model="model",
                session_id="s",
                prompt="p",
                messages=[ChatMessage(role="user", content="p")],
                request_id="r",
                use_agent=False,
                use_web_search=False,
            )
    finally:
        await client.aclose()

    assert "503" in str(exc_info.value)
    assert "upstream-secret-details" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_client_treats_structured_chat_bad_gateway_as_retryable() -> None:
    """带应用前缀的聊天接口返回脱敏 502 时，客户端应保留可重试分类。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        """模拟 Java 聊天异常过滤器的公开协议。"""

        assert request.url.path == "/dispatch-ai/chat/chatOne"
        return httpx.Response(
            502, json={"code": 502, "description": "AI 服务暂时不可用", "data": None},
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DispatchAiUnavailableError) as exc_info:
            await client.complete(
                model="model", session_id="s", prompt="p",
                messages=[ChatMessage(role="user", content="p")], request_id="r",
                use_agent=False, use_web_search=False,
            )
    finally:
        await client.aclose()

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [(400, DispatchAiRejectedError), (502, DispatchAiUnavailableError)],
)
async def test_chat_agent_classifies_structured_http_errors(
    status: int, error_type: type[Exception],
) -> None:
    """异步 Agent 的参数错误不可重试，上游不可用才进入重试分类。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        """模拟 Java 异步异常边界发出的真实 HTTP 状态与错误包装。"""
        assert request.url.path == "/dispatch-ai/chat/chatAgentOne"
        return httpx.Response(
            status, json={"code": status, "description": "tools 不能为空", "data": None},
        )

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(error_type) as exc_info:
            await client.complete(
                model="model", session_id="s", prompt="p",
                messages=[ChatMessage(role="user", content="p")], request_id="r",
                chat_tools=("memory",), tool_session_token="t" * 32,
                use_web_search=False,
            )
    finally:
        await client.aclose()

    assert exc_info.value.status_code == status


@pytest.mark.asyncio
async def test_client_sends_configured_general_mcp_services_to_agent_chat() -> None:
    """普通 Agent 即使关闭联网搜索，也应携带管理员配置的 MCP 服务。"""

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert request.url.path.endswith("/chat/agentChat")
        assert payload["mcpServices"] == ["windows-mcp"]
        assert "tools" not in payload
        assert payload["maxSteps"] == 3
        return _agent_result_response("已读取 Windows 状态")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        mcp_services=("windows-mcp",),
        transport=httpx.MockTransport(handler),
    )

    try:
        answer = await client.complete(
            model="model",
            session_id="s",
            prompt="查看当前窗口",
            messages=[ChatMessage(role="user", content="查看当前窗口")],
            request_id="r",
            use_agent=True,
            use_web_search=False,
        )
    finally:
        await client.aclose()

    assert answer == "已读取 Windows 状态"


@pytest.mark.asyncio
async def test_client_maps_business_rejection() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 429, "description": "busy", "data": None})

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(DispatchAiRejectedError, match="429"):
            await client.complete(
                model="model",
                session_id="s",
                prompt="p",
                messages=[ChatMessage(role="user", content="p")],
                request_id="r",
                use_agent=False,
                use_web_search=False,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"code": 200, "data": None},
        {"code": 200, "data": {"replayContext": ""}},
        {"code": 200, "data": {"content": "wrong field"}},
    ],
)
async def test_client_rejects_invalid_success_payload(body: dict[str, object]) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(DispatchAiProtocolError):
            await client.complete(
                model="model",
                session_id="s",
                prompt="p",
                messages=[ChatMessage(role="user", content="p")],
                request_id="r",
                use_agent=False,
                use_web_search=False,
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_maps_timeout() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(DispatchAiUnavailableError, match="timed out"):
            await client.complete(
                model="model",
                session_id="s",
                prompt="p",
                messages=[ChatMessage(role="user", content="p")],
                request_id="r",
            )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_client_maps_connect_error_without_exposing_details() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("internal-hostname", request=request)

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(DispatchAiUnavailableError) as exc_info:
            await client.complete(
                model="model",
                session_id="s",
                prompt="p",
                messages=[ChatMessage(role="user", content="p")],
                request_id="r",
            )
    finally:
        await client.aclose()

    assert "internal-hostname" not in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, DispatchAiRejectedError),
        (429, DispatchAiUnavailableError),
    ],
)
async def test_client_classifies_http_statuses(
    status: int,
    error_type: type[Exception],
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="sensitive response body")

    client = DispatchAiClient(
        base_url="http://ai.test/dispatch-ai",
        timeout_seconds=3,
        transport=httpx.MockTransport(handler),
    )

    try:
        with pytest.raises(error_type) as exc_info:
            await client.complete(
                model="model",
                session_id="s",
                prompt="p",
                messages=[ChatMessage(role="user", content="p")],
                request_id="r",
            )
    finally:
        await client.aclose()

    assert str(status) in str(exc_info.value)
    assert exc_info.value.status_code == status
    assert "sensitive response body" not in str(exc_info.value)
