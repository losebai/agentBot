"""模型路由与媒体队列使用的能力类别。"""

from __future__ import annotations

from enum import Enum

from agent_bot.models import MediaOutputType


class ContentCategory(str, Enum):
    """文本、识图与显式媒体任务所需的模型能力。"""

    TEXT = "TEXT"
    IMAGE_UNDERSTANDING = "IMAGE_UNDERSTANDING"
    IMAGE_GENERATION = "IMAGE_GENERATION"
    AUDIO_GENERATION = "AUDIO_GENERATION"

    @property
    def media_output(self) -> MediaOutputType | None:
        if self is ContentCategory.IMAGE_GENERATION:
            return "IMAGE"
        if self is ContentCategory.AUDIO_GENERATION:
            return "AUDIO"
        return None
