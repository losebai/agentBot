"""聊天路由与后台压缩的行为回归。"""

import asyncio

import pytest

from agent_bot.models import ChatToolName
from agent_bot.state import ConversationTurn, MemoryCompression


@pytest.mark.parametrize("prompt", [
    "桃桃，来句语音我听听", "你好", "介绍一下语音回复功能",
    "不要发语音", "前面是一大段聊天背景。" * 5 + "让我听听你的声音",
])
def test_tts_authorization_is_independent_of_message_wording(prompt: str) -> None:
    """是否合成语音由模型选择；本地只限制授权工具集合。"""
    from agent_bot.services.chat.routing import ChatRouteSelector

    selector = ChatRouteSelector()
    assert selector.select(
        prompt, available_tools=("mention_member", "tts_generate"),
    ).chat_tools == ("mention_member", "tts_generate")
    assert selector.select(prompt, available_tools=()).chat_tools == ()


def test_local_workflow_authorization_is_independent_of_message_wording() -> None:
    """数据库配置提供画图工具后，每次请求都应交给模型自主选择。"""

    from agent_bot.services.chat.routing import ChatRouteSelector

    selector = ChatRouteSelector()
    assert selector.select(
        "你好，聊聊今天的心情",
        available_tools=("local_workflow_list", "local_workflow_generate_image"),
    ).chat_tools == ("local_workflow_list", "local_workflow_generate_image")
    assert selector.select(
        "画图可爱的萝莉",
        available_tools=("local_workflow_list", "local_workflow_generate_image"),
    ).chat_tools == ("local_workflow_list", "local_workflow_generate_image")
    assert selector.select("帮我画一张猫的图片", available_tools=()).chat_tools == ()


@pytest.mark.parametrize("web_search_enabled", [True, False])
@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (
            "你好，聊聊今天的心情",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "解释一下二分查找",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "我走8 8，轮到dd了",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "记得调用at的工具",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "调用工具mention_member啊",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "帮我搜一下今天的新闻",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "查一下 Solon 文档",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "总结 https://example.com/page",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "今天北京天气如何",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "帮我查玩家小白的战绩",
            (
                "naraka_query", "naraka_search", "mention_member", "local_workflow_list",
                "local_workflow_generate_image", "tts_generate",
            ),
        ),
        (
            "提醒小白晚上集合",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "搜索今天新闻再提醒小白",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "帮我画一张猫的图片",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
        (
            "用语音回复你好",
            (
                "mention_member", "local_workflow_list", "local_workflow_generate_image",
                "tts_generate",
            ),
        ),
    ],
)
def test_route_keeps_authorized_mention_available_and_selects_other_tools(
    prompt: str, expected: tuple[str, ...], web_search_enabled: bool,
) -> None:
    """联网工具仅由配置控制，消息措辞和本地工具不影响其可用性。"""

    from agent_bot.services.chat.routing import ChatRouteSelector

    allowed: tuple[ChatToolName, ...] = (
        "naraka_query", "naraka_search", "mention_member", "local_workflow_list",
        "local_workflow_generate_image", "tts_generate",
    )
    route = ChatRouteSelector().select(
        prompt, available_tools=allowed, web_search_enabled=web_search_enabled,
    )
    assert route.chat_tools == expected
    assert route.web_search is web_search_enabled
    assert route.use_agent is bool(expected or web_search_enabled)
    assert ChatRouteSelector().select(prompt, available_tools=()).chat_tools == ()


@pytest.mark.parametrize("web_search_enabled", [True, False])
@pytest.mark.parametrize("prompt", ["你好", "我走8 8", "记得调用at的工具"])
def test_route_does_not_grant_mention_when_unavailable(
    prompt: str, web_search_enabled: bool,
) -> None:
    """联网能力可单独进入 Agent，但不会授予未授权的本地工具。"""

    from agent_bot.services.chat.routing import ChatRouteSelector

    route = ChatRouteSelector().select(
        prompt,
        available_tools=("naraka_query", "naraka_search"),
        web_search_enabled=web_search_enabled,
    )
    assert route.chat_tools == ()
    assert route.web_search is web_search_enabled
    assert route.use_agent is web_search_enabled


def compression(revision: int, session_id: str = "s") -> MemoryCompression:
    """创建内容明确且版本不同的压缩快照。"""

    return MemoryCompression(
        session_id, revision, "", (ConversationTurn("u", "a"),), generation_id="a" * 32,
    )


@pytest.mark.asyncio
async def test_compression_is_background_and_coalesces_latest_session_revision() -> None:
    """压缩在后台执行，积压的同会话快照只保留最新一份。"""

    from agent_bot.services.chat.compression_queue import ChatCompressionQueue

    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()
    seen = []

    async def compress(snapshot: MemoryCompression) -> None:
        """阻塞第一次模型调用，观察后续合并结果。"""

        seen.append(snapshot.revision)
        if snapshot.revision == 1:
            started.set()
            await release.wait()
        if snapshot.revision == 3:
            finished.set()

    queue = ChatCompressionQueue(compress, capacity=2)
    try:
        assert queue.submit(compression(1))
        await asyncio.wait_for(started.wait(), 1)
        assert queue.submit(compression(2))
        assert queue.submit(compression(3))
        assert seen == [1]
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        assert seen == [1, 3]
    finally:
        await queue.aclose()


@pytest.mark.asyncio
async def test_compression_capacity_and_close_cancel_inflight_work() -> None:
    """容量饱和不会扩张后台任务，关闭会取消在途模型调用。"""

    from agent_bot.services.chat.compression_queue import ChatCompressionQueue

    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def compress(_snapshot: MemoryCompression) -> None:
        """模拟不能自行完成的模型请求。"""

        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    queue = ChatCompressionQueue(compress, capacity=1)
    assert queue.submit(compression(1))
    await asyncio.wait_for(started.wait(), 1)
    assert queue.submit(compression(2, "pending"))
    assert not queue.submit(compression(3, "overflow"))
    await queue.aclose()
    assert cancelled.is_set()
    assert not queue.submit(compression(4))


@pytest.mark.asyncio
async def test_compression_failure_does_not_stop_other_sessions() -> None:
    """一个会话压缩失败不终止后台队列。"""

    from agent_bot.services.chat.compression_queue import ChatCompressionQueue

    finished = asyncio.Event()

    async def compress(snapshot: MemoryCompression) -> None:
        """第一份快照失败，第二份完成。"""

        if snapshot.session_id == "bad":
            raise RuntimeError("offline failure")
        finished.set()

    queue = ChatCompressionQueue(compress, capacity=2)
    try:
        queue.submit(compression(1, "bad"))
        queue.submit(compression(1, "good"))
        await asyncio.wait_for(finished.wait(), 1)
    finally:
        await queue.aclose()


@pytest.mark.asyncio
async def test_new_session_generation_replaces_pending_old_compression() -> None:
    """清空后的新代际不因旧快照版本更大而被丢弃。"""

    from dataclasses import replace

    from agent_bot.services.chat.compression_queue import ChatCompressionQueue

    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    seen: list[MemoryCompression] = []

    async def compress(snapshot: MemoryCompression) -> None:
        """阻塞首个会话，以构造另一会话的压缩积压。"""

        if snapshot.session_id == "blocking":
            started.set()
            await release.wait()
        else:
            seen.append(snapshot)
            completed.set()

    queue = ChatCompressionQueue(compress, capacity=2)
    try:
        queue.submit(compression(1, "blocking"))
        await asyncio.wait_for(started.wait(), 1)
        queue.submit(compression(5))
        latest = replace(compression(1), generation_id="b" * 32)
        queue.submit(latest)
        release.set()
        await asyncio.wait_for(completed.wait(), 1)
        assert seen == [latest]
    finally:
        await queue.aclose()
