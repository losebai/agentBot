"""模型响应与持久投递共用的图片附件校验。"""

from __future__ import annotations

import base64
import binascii
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ImageArtifact(BaseModel):
    """Redis 中持久化、供 NapCat 重试投递的已验证图片。"""

    model_config = ConfigDict(frozen=True, populate_by_name=True, extra="forbid")

    mime_type: Literal["image/png", "image/jpeg", "image/webp", "image/gif"] = Field(
        alias="mimeType"
    )
    data: str = Field(min_length=1, max_length=13_981_016, repr=False)

    @field_validator("data")
    @classmethod
    def validate_base64(cls, value: str) -> str:
        """拒绝非法或超过单请求硬上限的 Base64。"""

        try:
            raw = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("workflow image Base64 is invalid") from exc
        if not raw or len(raw) > 10_485_760:
            raise ValueError("workflow image size is invalid")
        return value

    @model_validator(mode="after")
    def validate_signature(self) -> ImageArtifact:
        """核对媒体类型与真实图片魔数。"""

        raw = base64.b64decode(self.data, validate=True)
        valid = (
            (self.mime_type == "image/png"
            and raw.startswith(b"\x89PNG\r\n\x1a\n"))
            or (self.mime_type == "image/jpeg"
            and raw.startswith(b"\xff\xd8\xff"))
            or (self.mime_type == "image/webp"
            and len(raw) >= 12
            and raw.startswith(b"RIFF")
            and raw[8:12] == b"WEBP")
            or (self.mime_type == "image/gif"
            and raw.startswith((b"GIF87a", b"GIF89a")))
        )
        if not valid:
            raise ValueError("workflow image signature is invalid")
        return self
