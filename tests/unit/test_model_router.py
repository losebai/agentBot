import pytest

from agent_bot.content_classifier import ContentCategory
from agent_bot.model_router import ContentModelRouter


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ContentCategory.TEXT, "chat-model"),
        (ContentCategory.IMAGE_UNDERSTANDING, "vision-model"),
        (ContentCategory.IMAGE_GENERATION, "image-model"),
        (ContentCategory.AUDIO_GENERATION, "audio-model"),
    ],
)
def test_router_returns_independent_model_for_each_category(
    category: ContentCategory,
    expected: str,
) -> None:
    router = ContentModelRouter(
        text_model="chat-model",
        vision_model="vision-model",
        image_model="image-model",
        audio_model="audio-model",
    )

    assert router.model_for(category) == expected
