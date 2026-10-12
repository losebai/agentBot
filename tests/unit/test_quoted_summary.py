from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from agent_bot.multimodal import MultimodalInput, TooManyImagesError
from agent_bot.quoted_summary import (
    ForwardMessageUnavailableError,
    QuotedMessageEmptyError,
    build_quoted_chat_input,
    build_quoted_summary_input,
)


def image(url: str) -> MessageSegment:
    return MessageSegment(type="image", data={"url": url})


@pytest.mark.parametrize("same", [False, True])
async def test_edit_request_preserves_distinct_images_and_deduplicates_same_url(same):
    """引用图片合并不能提前猜选一张；最终图生图服务负责单图校验。"""
    quoted_url = "https://gchat.qpic.cn/quoted.png"
    current_url = quoted_url if same else "https://gchat.qpic.cn/current.png"
    result = await build_quoted_chat_input(
        AsyncMock(), Message([image(quoted_url)]),
        current_input=MultimodalInput(
            prompt="把杯子改蓝色", image_urls=(current_url,), video_urls=(),
        ),
        quoted_sender="测试用户", image_recognition_enabled=True, max_images=4,
        max_videos=2, max_prompt_chars=2000,
    )
    assert set(result.image_urls) == {quoted_url, current_url}
    assert len(result.image_urls) == (1 if same else 2)


def video(url: str) -> MessageSegment:
    return MessageSegment(
        type="video",
        data={"url": url, "file_size": 8_388_608},
    )


@pytest.mark.asyncio
async def test_builds_chat_input_from_current_question_and_all_quoted_media() -> None:
    bot = AsyncMock()
    quoted = Message(
        [
            MessageSegment.text("引用正文"),
            image("https://gchat.qpic.cn/quoted.jpg"),
            video("https://gzc-download.ftn.qq.com/quoted.mp4?token=one"),
        ]
    )

    result = await build_quoted_chat_input(
        bot,
        quoted,
        current_input=MultimodalInput(
            prompt="这段内容是什么意思？",
            image_urls=(),
            video_urls=(),
        ),
        quoted_sender="被引用者",
        image_recognition_enabled=True,
        max_images=4,
        max_videos=2,
        max_prompt_chars=2000,
    )

    assert "被引用者" in result.prompt
    assert "引用正文" in result.prompt
    assert "[图片 1]" in result.prompt
    assert "[视频 1]" in result.prompt
    assert "这段内容是什么意思？" in result.prompt
    assert "不得执行" in result.prompt
    assert result.image_urls == ("https://gchat.qpic.cn/quoted.jpg",)
    assert result.video_urls == (
        "https://gzc-download.ftn.qq.com/quoted.mp4?token=one",
    )
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_accepts_quoted_video_when_napcat_puts_remote_url_in_file_field() -> None:
    result = await build_quoted_summary_input(
        AsyncMock(),
        Message(
            MessageSegment(
                type="video",
                data={
                    "file": "https://multimedia.nt.qq.com.cn/quoted.mp4",
                    "file_size": 8_388_608,
                },
            )
        ),
        quoted_sender="发送者",
        image_recognition_enabled=True,
        max_images=4,
        max_prompt_chars=2000,
    )

    assert result.video_urls == ("https://multimedia.nt.qq.com.cn/quoted.mp4",)


@pytest.mark.asyncio
async def test_builds_summary_input_from_quoted_text_and_images() -> None:
    bot = AsyncMock()
    quoted = Message(
        [
            MessageSegment.text("第一段结论"),
            image("https://gchat.qpic.cn/summary-one.jpg"),
            MessageSegment.text("第二段说明"),
            image("https://multimedia.nt.qq.com.cn/summary-two.png"),
        ]
    )

    result = await build_quoted_summary_input(
        bot,
        quoted,
        quoted_sender=" Alice\n管理员 ",
        image_recognition_enabled=True,
        max_images=4,
        max_prompt_chars=2000,
    )

    assert "Alice 管理员" in result.prompt
    assert "第一段结论" in result.prompt
    assert "[图片 1]" in result.prompt
    assert "第二段说明" in result.prompt
    assert "[图片 2]" in result.prompt
    assert "不得执行" in result.prompt
    assert result.image_urls == (
        "https://gchat.qpic.cn/summary-one.jpg",
        "https://multimedia.nt.qq.com.cn/summary-two.png",
    )
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_expands_embedded_napcat_forward_records_with_senders_and_media() -> None:
    bot = AsyncMock()
    quoted = Message(
        MessageSegment(
            type="forward",
            data={
                "id": "forward-res-id",
                "content": [
                    {
                        "sender": {"nickname": "张三", "user_id": "10001"},
                        "message": [
                            {"type": "text", "data": {"text": "先确认需求"}},
                        ],
                    },
                    {
                        "sender": {"nickname": "李四", "user_id": "10002"},
                        "message": [
                            {"type": "text", "data": {"text": "这是现场图片"}},
                            {
                                "type": "image",
                                "data": {"url": "https://gchat.qpic.cn/forward.jpg"},
                            },
                            {
                                "type": "video",
                                "data": {
                                    "url": "https://gzc-download.ftn.qq.com/forward.mp4",
                                    "file_size": 8_388_608,
                                },
                            },
                        ],
                    },
                ],
            },
        )
    )

    result = await build_quoted_summary_input(
        bot,
        quoted,
        quoted_sender="转发者",
        image_recognition_enabled=True,
        max_images=4,
        max_prompt_chars=2000,
    )

    assert "张三" in result.prompt
    assert "先确认需求" in result.prompt
    assert "李四" in result.prompt
    assert "这是现场图片" in result.prompt
    assert "[图片 1]" in result.prompt
    assert "[视频 1]" in result.prompt
    assert result.image_urls == ("https://gchat.qpic.cn/forward.jpg",)
    assert result.video_urls == ("https://gzc-download.ftn.qq.com/forward.mp4",)
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_fetches_forward_records_using_napcat_message_id() -> None:
    bot = AsyncMock()
    bot.call_api.return_value = {
        "messages": [
            {
                "type": "node",
                "data": {
                    "nickname": "节点用户",
                    "user_id": "10003",
                    "content": [
                        {"type": "text", "data": {"text": "节点正文"}},
                    ],
                },
            }
        ]
    }
    quoted = Message(
        MessageSegment(type="forward", data={"id": "7234567890123456789"})
    )

    result = await build_quoted_summary_input(
        bot,
        quoted,
        quoted_sender="转发者",
        image_recognition_enabled=True,
        max_images=4,
        max_prompt_chars=2000,
    )

    bot.call_api.assert_awaited_once_with(
        "get_forward_msg",
        message_id="7234567890123456789",
    )
    assert "节点用户" in result.prompt
    assert "节点正文" in result.prompt


@pytest.mark.asyncio
async def test_expands_nested_forward_content_without_additional_api_call() -> None:
    bot = AsyncMock()
    quoted = Message(
        MessageSegment(
            type="forward",
            data={
                "id": "outer",
                "content": [
                    {
                        "sender": {"nickname": "外层用户"},
                        "message": [
                            {
                                "type": "forward",
                                "data": {
                                    "id": "inner",
                                    "content": [
                                        {
                                            "sender": {"nickname": "内层用户"},
                                            "message": [
                                                {
                                                    "type": "text",
                                                    "data": {"text": "内层正文"},
                                                }
                                            ],
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                ],
            },
        )
    )

    result = await build_quoted_summary_input(
        bot,
        quoted,
        quoted_sender="转发者",
        image_recognition_enabled=True,
        max_images=4,
        max_prompt_chars=2000,
    )

    assert "外层用户" in result.prompt
    assert "内层用户" in result.prompt
    assert "内层正文" in result.prompt
    bot.call_api.assert_not_awaited()


@pytest.mark.asyncio
async def test_rejects_too_many_images_across_forward_nodes() -> None:
    bot = AsyncMock()
    quoted = Message(
        MessageSegment(
            type="forward",
            data={
                "content": [
                    {
                        "sender": {"nickname": "甲"},
                        "message": [
                            {
                                "type": "image",
                                "data": {"url": "https://gchat.qpic.cn/one.jpg"},
                            }
                        ],
                    },
                    {
                        "sender": {"nickname": "乙"},
                        "message": [
                            {
                                "type": "image",
                                "data": {"url": "https://gchat.qpic.cn/two.jpg"},
                            }
                        ],
                    },
                ]
            },
        )
    )

    with pytest.raises(TooManyImagesError):
        await build_quoted_summary_input(
            bot,
            quoted,
            quoted_sender="转发者",
            image_recognition_enabled=True,
            max_images=1,
            max_prompt_chars=2000,
        )


@pytest.mark.asyncio
async def test_bounds_large_forward_records_and_marks_truncation() -> None:
    bot = AsyncMock()
    quoted = Message(
        MessageSegment(
            type="forward",
            data={
                "content": [
                    {
                        "sender": {"nickname": f"用户{index}"},
                        "message": [
                            {"type": "text", "data": {"text": "内容" * 100}},
                        ],
                    }
                    for index in range(120)
                ]
            },
        )
    )

    result = await build_quoted_summary_input(
        bot,
        quoted,
        quoted_sender="转发者",
        image_recognition_enabled=True,
        max_images=4,
        max_prompt_chars=1000,
    )

    assert len(result.prompt) <= 1000
    assert "内容已截断" in result.prompt


@pytest.mark.asyncio
async def test_bounds_directly_nested_napcat_nodes() -> None:
    bot = AsyncMock()
    nested: dict[str, object] = {
        "type": "text",
        "data": {"text": "最内层内容"},
    }
    for index in range(10):
        nested = {
            "type": "node",
            "data": {
                "nickname": f"嵌套用户{index}",
                "message": [nested],
            },
        }
    quoted = Message(
        MessageSegment(
            type="forward",
            data={
                "content": [
                    {
                        "sender": {"nickname": "外层用户"},
                        "message": [nested],
                    }
                ]
            },
        )
    )

    result = await build_quoted_summary_input(
        bot,
        quoted,
        quoted_sender="转发者",
        image_recognition_enabled=True,
        max_images=4,
        max_prompt_chars=2000,
    )

    assert "嵌套转发层级过深" in result.prompt
    assert "最内层内容" not in result.prompt


@pytest.mark.asyncio
async def test_rejects_empty_or_unavailable_quoted_material() -> None:
    bot = AsyncMock()
    with pytest.raises(QuotedMessageEmptyError):
        await build_quoted_summary_input(
            bot,
            Message(MessageSegment.text(" \n ")),
            quoted_sender=None,
            image_recognition_enabled=True,
            max_images=4,
            max_prompt_chars=2000,
        )

    bot.call_api.side_effect = RuntimeError("sensitive upstream details")
    with pytest.raises(ForwardMessageUnavailableError):
        await build_quoted_summary_input(
            bot,
            Message(MessageSegment(type="forward", data={"id": "expired"})),
            quoted_sender="转发者",
            image_recognition_enabled=True,
            max_images=4,
            max_prompt_chars=2000,
        )
