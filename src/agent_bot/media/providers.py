"""直接图片与对话音频协议，不自动改用 TTS、URL 下载或其他后端。"""

import base64
import binascii

from pydantic import ValidationError

from agent_bot.ai.providers.transport import ProviderTransport
from agent_bot.media.errors import MediaError
from agent_bot.media.validation import AUDIO_MIME, IMAGE_MIME
from agent_bot.models import ChatMediaItem, ChatMediaResponse


def validate_generation_request(request, resolved):
    """显式能力决定可用组合，在任何上游请求前执行。"""
    model, protocol = resolved.model, resolved.provider.protocol
    if not model.media_initialized:
        raise MediaError("MEDIA_NOT_INITIALIZED", status_code=503)
    if protocol == "ollama":
        raise MediaError("MEDIA_PROTOCOL_UNSUPPORTED", status_code=422)
    if request.output_type == "IMAGE":
        if model.media.image_api_profile == "none":
            raise MediaError("MEDIA_IMAGE_UNSUPPORTED", status_code=422)
        if model.media.image_api_profile == "legacy-b64" and request.image_format != "png":
            raise MediaError("MEDIA_FORMAT_UNSUPPORTED", status_code=422)
    elif not model.media.audio_chat_enabled:
        raise MediaError("MEDIA_AUDIO_UNSUPPORTED", status_code=422)


class MediaProvider:
    """拥有独立的有界 HTTP 客户端，任何生成副作用只调用一次。"""

    def __init__(self, transport=None):
        """允许测试注入网络替身，不包含供应商选择或后备路由。"""
        self.transport = transport or ProviderTransport()

    async def generate(self, request, resolved, credential):
        """编码固定端点请求并校验唯一媒体项，真实图片解码由用例线程完成。"""
        validate_generation_request(request, resolved)
        model = resolved.model
        if request.output_type == "IMAGE":
            path = "/images/generations"
            payload = {"model": model.upstream_model, "prompt": request.prompt, "n": 1}
            if model.media.image_api_profile == "gpt-image":
                payload["output_format"] = request.image_format
            else:
                payload["response_format"] = "b64_json"
        else:
            path = "/chat/completions"
            payload = {
                "model": model.upstream_model,
                "messages": [m.model_dump() for m in request.messages],
                "stream": False,
                "modalities": ["text", "audio"],
                "audio": {"voice": request.voice, "format": request.audio_format},
            }
        headers = {"Accept": "application/json"}
        if resolved.provider.user_agent:
            headers["User-Agent"] = resolved.provider.user_agent
        if credential is not None:
            headers["Authorization"] = "Bearer " + credential.get_secret_value()
        value = await self.transport.read_json(
            resolved.provider.base_url + path,
            payload=payload,
            headers=headers,
            timeout_seconds=request.timeout_seconds,
            maximum_bytes=4 * ((request.max_bytes + 2) // 3) + 65536,
        )
        try:
            if request.output_type == "IMAGE":
                data = value["data"]
                if not isinstance(data, list) or len(data) != 1 or "url" in data[0]:
                    raise ValueError("invalid image result")
                encoded, text, mime = data[0]["b64_json"], None, IMAGE_MIME[request.image_format]
            else:
                choices = value["choices"]
                if len(choices) != 1 or choices[0].get("finish_reason") != "stop":
                    raise ValueError("invalid audio finish")
                message = choices[0]["message"]
                if (
                    message.get("role") != "assistant"
                    or message.get("tool_calls")
                    or message.get("refusal")
                ):
                    raise ValueError("invalid audio message")
                encoded = message["audio"]["data"]
                text = message.get("content") or message["audio"].get("transcript")
                mime = AUDIO_MIME[request.audio_format]
            if not isinstance(encoded, str) or len(encoded) > 4 * ((request.max_bytes + 2) // 3):
                raise ValueError("invalid encoded size")
            raw = base64.b64decode(encoded, validate=True)
            if not raw or len(raw) > request.max_bytes:
                raise ValueError("invalid decoded size")
            return ChatMediaResponse(
                text=text,
                items=(ChatMediaItem(type=request.output_type, mime_type=mime, data=encoded),),
            )
        except (
            KeyError,
            TypeError,
            ValueError,
            binascii.Error,
            ValidationError,
            IndexError,
        ) as error:
            raise MediaError("MEDIA_INVALID_RESPONSE") from error

    async def aclose(self):
        """仅关闭本对象拥有的 HTTP 传输。"""
        await self.transport.aclose()
