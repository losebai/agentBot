import pytest

from agent_bot.content_classifier import ContentCategory


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        (ContentCategory.TEXT, None),
        (ContentCategory.IMAGE_UNDERSTANDING, None),
        (ContentCategory.IMAGE_GENERATION, "IMAGE"),
        (ContentCategory.AUDIO_GENERATION, "AUDIO"),
    ],
)
def test_content_category_exposes_existing_queue_media_output(
    category: ContentCategory,
    expected: str | None,
) -> None:
    assert category.media_output == expected
