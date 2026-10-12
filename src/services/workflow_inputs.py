"""只允许网关生成的、按请求隔离的 Comfy 原图引用。"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def workflow_input_scope(source_job_id: str) -> str:
    """与后台 image 任务 ID 共用请求摘要，避免同 session 并行串图。"""
    return hashlib.sha256(source_job_id.encode("utf-8")).hexdigest()[:32]


class WorkflowInputImage(BaseModel):
    """已验证上传响应的输入引用；不接受 URL、绝对路径或图片字节。"""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    filename: str = Field(pattern=r"^[a-f0-9]{64}\.(png|jpg|webp)$")
    subfolder: str = Field(pattern=r"^dispatch-chat-i2i/[a-f0-9]{32}$")
    media_type: Literal["image/png", "image/jpeg", "image/webp"]
    size_bytes: int = Field(gt=0, le=10_485_760)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @model_validator(mode="after")
    def validate_content_filename(self) -> WorkflowInputImage:
        """文件名必须与原图摘要和实际 MIME 一致。"""
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
        if self.filename != f"{self.sha256}.{extension[self.media_type]}":
            raise ValueError("input image content reference is invalid")
        return self

    @property
    def comfy_path(self) -> str:
        """组合已经白名单校验的 Comfy 相对输入路径。"""
        return f"{self.subfolder}/{self.filename}"
