"""管理媒体请求只接受用户可选字段，身份和资源预算由服务器创建。"""

from typing import Literal

from pydantic import Field, model_validator

from agent_bot.admin.models import ApiModel
from agent_bot.materials.contracts import MaterialId
from agent_bot.media.contracts import MediaAssetView
from agent_bot.models import ChatMessage


class MaterialUploadView(MediaAssetView):
    """上传保留临时执行附件，同时返回可供记忆关联的持久素材身份。"""

    material_id: MaterialId


class MediaGenerationProbeRequest(ApiModel):
    """管理端直接图片或对话音频输入，不允许覆盖地址、凭据或运行限制。"""

    model: str = Field(min_length=1, max_length=200)
    session_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    prompt: str = Field(min_length=1, max_length=2000)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=24)
    output_type: Literal["IMAGE", "AUDIO"]
    image_format: Literal["png", "jpeg", "webp"] | None = None
    voice: str | None = Field(default=None, min_length=1, max_length=255)
    audio_format: Literal["mp3", "opus", "aac", "flac", "wav", "pcm"] | None = None

    @model_validator(mode="after")
    def check_conversation(self):
        """历史禁止系统角色且严格限量，图像和音频参数不能混用。"""
        if (
            not self.prompt.strip()
            or not self.model.strip()
            or any(m.role == "system" for m in self.messages)
            or sum(len(m.content) for m in self.messages) > 12000
        ):
            raise ValueError("invalid media conversation")
        if self.output_type == "IMAGE" and (
            self.messages or self.voice is not None or self.audio_format is not None
        ):
            raise ValueError("unexpected audio parameters")
        if self.output_type == "AUDIO" and self.image_format is not None:
            raise ValueError("unexpected image parameters")
        return self
