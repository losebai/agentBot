"""角色图片只接收有界静态位图，校验后作为专属二进制数据随角色事务保存。"""

import base64
import binascii
import hashlib

from pydantic import Field

from agent_bot.ai.contracts import AiModel
from agent_bot.media.errors import MediaError
from agent_bot.media.inputs import ImagePreparer

MAX_PORTRAIT_BYTES = 5 * 1024 * 1024


class PortraitUpload(AiModel):
    """管理端上传原图，字节和真实容器均由服务器验证，不接受任意 URL。"""

    mime_type: str = Field(pattern=r"^image/(png|jpeg|webp)$")
    data: str = Field(max_length=4 * ((MAX_PORTRAIT_BYTES + 2) // 3), repr=False)

    def decode(self) -> tuple[bytes, str]:
        """拒绝损坏、伪装和动画图片，返回完整性摘要供展示更新缓存。"""
        try:
            raw = base64.b64decode(self.data, validate=True)
        except (ValueError, binascii.Error) as error:
            raise MediaError("MEDIA_INVALID_IMAGE", status_code=422) from error
        if not raw or len(raw) > MAX_PORTRAIT_BYTES:
            raise MediaError("MEDIA_TOO_LARGE", status_code=413)
        ImagePreparer().prepare(raw, self.mime_type)
        return raw, hashlib.sha256(raw).hexdigest()
