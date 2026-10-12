"""直接图片和对话音频的固定协议、参数与结果边界。"""

import base64
import json

import httpx
import pytest

from agent_bot.ai.contracts import ModelMediaCapabilities
from agent_bot.ai.providers.transport import ProviderTransport
from agent_bot.media.errors import MediaError
from agent_bot.models import ChatMediaRequest, ChatMessage
from ai.test_providers import call
from media.test_inputs import picture


def resolved(profile="gpt-image", *, audio=True, protocol="openai-chat"):
    """构造启用能力的模型快照，名称不决定能力。"""
    selected = call(protocol).resolved
    return selected.model_copy(
        update={
            "model": selected.model.model_copy(
                update={
                    "media_initialized": True,
                    "media": ModelMediaCapabilities(
                        image_api_profile=profile, audio_chat_enabled=audio
                    ),
                }
            )
        }
    )


def media_request(kind="IMAGE", format="png"):
    """使用真实统一请求契约，不向上游传入任意服务地址。"""
    fields = (
        {"prompt": "画一只猫", "image_format": format}
        if kind == "IMAGE"
        else {
            "messages": (ChatMessage(role="user", content="你看到了什么？"),),
            "voice": "alloy",
            "audio_format": format,
        }
    )
    return ChatMediaRequest(
        request_id="r",
        session_id="s",
        user="u",
        model="alias",
        output_type=kind,
        timeout_seconds=120,
        max_bytes=1024,
        **fields,
    )


@pytest.mark.parametrize(
    "profile,format",
    [("gpt-image", "png"), ("gpt-image", "jpeg"), ("gpt-image", "webp"), ("legacy-b64", "png")],
)
async def test_image_payload_profiles(profile, format):
    """两种图片接口参数互不混用，响应固定要求内联 Base64。"""
    from agent_bot.media.providers import MediaProvider

    sent = []
    raw = picture({"png": "PNG", "jpeg": "JPEG", "webp": "WEBP"}[format])

    def handle(request):
        """模拟供应商生成响应，记录负载供结构断言。"""
        sent.append(json.loads(request.content))
        assert request.url.path.endswith("/images/generations")
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(raw).decode()}]})

    provider = MediaProvider(ProviderTransport(httpx.MockTransport(handle)))
    try:
        result = await provider.generate(media_request(format=format), resolved(profile), None)
        assert base64.b64decode(result.items[0].data) == raw
        assert sent[0]["n"] == 1
        if profile == "gpt-image":
            assert sent[0]["output_format"] == format and "response_format" not in sent[0]
        else:
            assert sent[0]["response_format"] == "b64_json" and "output_format" not in sent[0]
    finally:
        await provider.aclose()


async def test_audio_uses_conversation_and_returns_transcript():
    """音频生成提交整段对话，不能替换成朗读用户问题的 TTS。"""
    from agent_bot.media.providers import MediaProvider

    sent = []

    def handle(request):
        """只接受对话端点，返回文字和可验证的 PCM 数据。"""
        assert request.url.path.endswith("/chat/completions")
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "audio": {"data": "AAE=", "transcript": "回答"},
                        },
                    }
                ]
            },
        )

    provider = MediaProvider(ProviderTransport(httpx.MockTransport(handle)))
    try:
        request = media_request("AUDIO", "pcm")
        result = await provider.generate(request, resolved(), None)
        assert result.text == "回答" and result.items[0].mime_type == "audio/pcm"
        assert sent[0]["modalities"] == ["text", "audio"] and sent[0]["stream"] is False
        assert sent[0]["messages"] == [m.model_dump() for m in request.messages]
    finally:
        await provider.aclose()


@pytest.mark.parametrize(
    "value",
    [
        {"data": [{"url": "https://arbitrary.invalid"}]},
        {"data": [{"b64_json": "?"}]},
        {"data": []},
        {"data": [{"b64_json": "AA=="}, {"b64_json": "AA=="}]},
    ],
)
async def test_invalid_generation_never_downloads_urls(value):
    """供应商返回错误格式时只失败，不发第二个请求或自动重试。"""
    from agent_bot.media.providers import MediaProvider

    count = 0

    def handle(request):
        """计数可以证明 URL 不会被跟随下载。"""
        nonlocal count
        count += 1
        return httpx.Response(200, json=value)

    provider = MediaProvider(ProviderTransport(httpx.MockTransport(handle)))
    try:
        with pytest.raises(MediaError):
            await provider.generate(media_request(), resolved(), None)
        assert count == 1
    finally:
        await provider.aclose()


async def test_unsupported_profile_does_not_call_provider():
    """不支持的协议和格式在产生计费副作用前拒绝。"""
    from agent_bot.media.providers import MediaProvider

    def reject(request):
        """预检失败时不能到达 HTTP 层。"""
        pytest.fail("unexpected upstream call")

    provider = MediaProvider(ProviderTransport(httpx.MockTransport(reject)))
    try:
        for request, model in [
            (media_request(format="jpeg"), resolved("legacy-b64")),
            (media_request(), resolved("none")),
            (media_request(), resolved(protocol="ollama")),
            (media_request("AUDIO", "pcm"), resolved(audio=False)),
        ]:
            with pytest.raises(MediaError):
                await provider.generate(request, model, None)
    finally:
        await provider.aclose()
