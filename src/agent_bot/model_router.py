"""把本地内容类别映射到运行时模型配置。"""

from __future__ import annotations

from dataclasses import dataclass

from agent_bot.content_classifier import ContentCategory


@dataclass(frozen=True, slots=True)
class ContentModelRouter:
    """保存四类能力的明确模型 ID。"""

    text_model: str
    vision_model: str
    image_model: str
    audio_model: str

    def model_for(self, category: ContentCategory) -> str:
        models = {
            ContentCategory.TEXT: self.text_model,
            ContentCategory.IMAGE_UNDERSTANDING: self.vision_model,
            ContentCategory.IMAGE_GENERATION: self.image_model,
            ContentCategory.AUDIO_GENERATION: self.audio_model,
        }
        return models[category]
