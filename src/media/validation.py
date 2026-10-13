"""校验生成结果的唯一性、严格 Base64、真实格式与字节预算。"""

import base64
import binascii

from agent_bot.media.config import MAX_IMAGE_BYTES
from agent_bot.media.errors import MediaError
from agent_bot.media.inputs import ImagePreparer
from agent_bot.media.signatures import matches_audio_signature
from agent_bot.models import ChatMediaResponse

IMAGE_MIME = {"png": "image/png", "jpeg": "image/jpeg", "webp": "image/webp"}
AUDIO_MIME = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm",
}


def validate_generated_media(
    response: ChatMediaResponse, *, output_type: str, format: str, max_bytes: int
) -> bytes:
    """附件必须与请求类型和格式完全一致，图片不能仅凭魔数通过。"""
    if output_type not in {"IMAGE", "AUDIO"} or len(response.items) != 1:
        raise MediaError("MEDIA_INVALID_RESPONSE")
    item = response.items[0]
    maximum = min(max_bytes, MAX_IMAGE_BYTES)
    expected = (IMAGE_MIME if output_type == "IMAGE" else AUDIO_MIME).get(format)
    if not expected or item.type != output_type or item.mime_type != expected:
        raise MediaError("MEDIA_INVALID_RESPONSE")
    if len(item.data) > 4 * ((maximum + 2) // 3):
        raise MediaError("MEDIA_TOO_LARGE")
    try:
        raw = base64.b64decode(item.data, validate=True)
    except (ValueError, binascii.Error) as error:
        raise MediaError("MEDIA_INVALID_RESPONSE") from error
    if not raw or len(raw) > maximum:
        raise MediaError("MEDIA_TOO_LARGE")
    if output_type == "IMAGE":
        ImagePreparer().prepare(raw, expected)
    elif not matches_audio_signature(raw, format):
        raise MediaError("MEDIA_INVALID_RESPONSE")
    return raw
