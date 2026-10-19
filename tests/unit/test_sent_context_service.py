"""以真实离线日志验证聊天服务读取实际出站历史且保持文本模型路由。"""

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_bot.memory_store import ConversationMemoryPolicy, MySqlConversationMemoryStore
from agent_bot.model_router import ContentModelRouter
from agent_bot.models import ChatMessage
from agent_bot.sent_messages import SentMessageStore
from agent_bot.services.chat import ChatService
from agent_bot.settings import DatabaseParams
from agent_bot.state import ConversationStore, ConversationStoreProtocol


class RecordingModelClient:
    """记录模型边界的完整输入，不创建网络客户端或执行图片下载。"""

    def __init__(self) -> None:
        """默认返回固定正文，各用例可指定实际生成但尚未发送的结果。"""
        self.answer = "下一条回答"
        self.calls: list[dict[str, Any]] = []

    async def complete(self, *, messages: Sequence[ChatMessage], **options: Any) -> str:
        """保存不可变消息快照，使测试检查服务真正交给模型的角色与正文。"""
        self.calls.append(options | {"messages": tuple(messages)})
        return self.answer


def make_service(
    client: RecordingModelClient, memory: ConversationStoreProtocol,
    sent: SentMessageStore | None = None,
) -> ChatService:
    """组装真实聊天编排，只替换外部模型调用。"""
    return ChatService(
        client=client, store=memory, sent_messages=sent, system_prompt="",
        max_input_chars=10000, cooldown_seconds=0, max_concurrent_requests=2,
        web_search_enabled=False,
        model_router=ContentModelRouter(
            text_model="text-only", vision_model="current-image-vision",
            image_model="image-generation", audio_model="audio-generation",
        ),
    )


@pytest.fixture
def context_database(tmp_path):
    """同步准备独立库，避免在异步服务生命周期中阻塞读取初始化脚本。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "context.sqlite3"))
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.executescript(Path("sql/agent_bot_init.sqlite.sql").read_text("utf-8"))
        connection.executescript(Path("sql/legacy/session-context-v1.sqlite.sql").read_text("utf-8"))
    return params


@pytest.fixture
async def service_context(context_database):
    """日志和历史共享真实事务后端，退出时等待服务拥有的压缩任务结束。"""
    params = context_database

    def now() -> datetime:
        """固定离线时钟，避免测试执行速度影响会话有效期。"""
        return datetime(2026, 10, 19, tzinfo=UTC)

    memory = MySqlConversationMemoryStore(
        params, clock=now,
        policy=ConversationMemoryPolicy(
            history_turns=10, max_history_chars=10000, ttl_seconds=3600,
            memory_enabled=False, max_memory_chars=1000,
        ),
    )
    sent = SentMessageStore(params, clock=now)
    client = RecordingModelClient()
    service = make_service(client, memory, sent)
    try:
        yield service, client, memory, sent
    finally:
        await service.aclose()


async def test_ordinary_reply_receives_successful_notices_and_image_material_ids(service_context):
    """普通下一轮模型读取真实通知、图片素材和生成文件，而非助手合成摘要。"""
    service, client, _memory, sent = service_context
    generation = await service.history_generation("s")
    await service.commit_queued("s", "请画图并导出文件", "旧合成摘要", request_id="r1")
    await sent.record("s", generation, "a" * 64, "r1", "qq-1", "开始画图", ())
    await sent.record("s", generation, "b" * 64, "r1", "qq-2",
                      "[图片] material_id=image-original，名称=cat.png", ("image-original",))
    await sent.record("s", generation, "c" * 64, None, "qq-3", "服务已恢复", ())
    await sent.record("s", generation, "d" * 64, "r1", "qq-4",
                      "[文件] material_id=export-original，名称=画作说明.txt", ("export-original",))

    await service.generate_queued("s", "继续解释", request_id="r2")

    assert [(message.role, message.content) for message in client.calls[-1]["messages"]] == [
        ("user", "请画图并导出文件"), ("assistant", "开始画图"),
        ("assistant", "[图片] material_id=image-original，名称=cat.png"),
        ("assistant", "服务已恢复"),
        ("assistant", "[文件] material_id=export-original，名称=画作说明.txt"),
        ("user", "继续解释"),
    ]


async def test_sent_history_respects_disabled_history_policy(service_context):
    """关闭近期历史仍保存原始发送记录，但不能向模型重新注入历史。"""
    service, client, memory, sent = service_context
    generation = await service.history_generation("s")
    await sent.record("s", generation, "e" * 64, None, "qq-5", "旧的主动发言", ())
    memory.update_policy(ConversationMemoryPolicy(
        history_turns=0, max_history_chars=10000, ttl_seconds=3600,
        memory_enabled=False, max_memory_chars=1000,
    ))
    await service.generate_queued("s", "当前问题", request_id="r2")
    assert [message.content for message in client.calls[-1]["messages"]] == ["当前问题"]
    assert len(await sent.list_messages("s", generation)) == 1


async def test_sent_fragments_override_legacy_full_reply_and_pending_remainder(service_context):
    """旧摘要包含未发送余文时，只有成功发送片段可进入模型上下文。"""
    service, client, _memory, sent = service_context
    generation = await service.history_generation("s")
    await service.commit_queued("s", "分段回答", "已发送第一段\n未发送第二段", request_id="r1")
    await sent.record("s", generation, "a" * 64, "r1", "qq-1", "已发送第一段", ())
    await sent.claim_delivery("s", generation, "b" * 64, "r1", None, "未发送第二段", ())

    await service.generate_queued("s", "刚才发了什么", request_id="r2")

    assert [(message.role, message.content) for message in client.calls[-1]["messages"]] == [
        ("user", "分段回答"), ("assistant", "已发送第一段"), ("user", "刚才发了什么"),
    ]


async def test_normal_respond_journal_and_user_commit_do_not_duplicate_answer(service_context):
    """真实普通 respond 同时产生日志与配对轮次后，下一轮只看见一次回答。"""
    service, client, _memory, sent = service_context
    generation = await service.history_generation("s")
    client.answer = "成功投递的普通回答"

    async def delivery(answer: str) -> str:
        """只模拟已确认平台边界，日志和后续用户轮次均走真实生产仓储。"""
        request_id = client.calls[-1]["request_id"]
        await sent.claim_delivery("s", generation, "a" * 64, request_id, None, answer, ())
        await sent.finish_delivery("a" * 64, "qq-1")
        return answer

    await service.respond("s", "普通问题", delivery)
    await service.generate_queued("s", "接着说", request_id="next")

    assert [(message.role, message.content) for message in client.calls[-1]["messages"]] == [
        ("user", "普通问题"), ("assistant", "成功投递的普通回答"), ("user", "接着说"),
    ]


async def test_reset_hides_background_delivery_bound_to_old_generation(service_context):
    """重置后原后台结果可以确认投递，但不能重新出现于新会话输入。"""
    service, client, _memory, sent = service_context
    old_generation = await service.history_generation("s")
    await service.commit_queued("s", "旧图请求", "正在处理旧图", request_id="old-request")
    await sent.claim_delivery("s", old_generation, "a" * 64, "old-request", None,
                              "旧后台图片 material-old", ("material-old",))
    assert await service.reset("s")
    generation = await service.history_generation("s")
    assert generation != old_generation
    await sent.finish_delivery("a" * 64, "qq-old")
    await sent.record("s", generation, "b" * 64, None, "qq-new", "新会话通知", ())

    await service.generate_queued("s", "现在开始", request_id="new-request")

    assert [(message.role, message.content) for message in client.calls[-1]["messages"]] == [
        ("assistant", "新会话通知"), ("user", "现在开始"),
    ]


async def test_proactive_provider_returns_assistants_without_fake_user(service_context):
    """主动发言共用的上下文提供器返回独立助手事件，不补造历史用户。"""
    service, client, _memory, sent = service_context
    generation = await service.history_generation("s")
    await sent.record("s", generation, "a" * 64, None, "qq-1", "主动问候", ())
    await sent.record("s", generation, "b" * 64, "background", "qq-2",
                      "后台文件 material-report", ("material-report",))

    context = await service.conversation_context("s")
    assert context.generation_id == generation
    assert [(message.role, message.content) for message in context.messages] == [
        ("assistant", "主动问候"), ("assistant", "后台文件 material-report"),
    ]
    await service.generate_queued("s", "今天怎么样", request_id="current")
    assert [message.content for message in client.calls[-1]["messages"]
            if message.role == "user"] == ["今天怎么样"]


async def test_history_image_metadata_does_not_select_vision_route(service_context):
    """历史图片仅作为文字引用，只有本次显式图片输入才能选择视觉模型。"""
    service, client, _memory, sent = service_context
    generation = await service.history_generation("s")
    await sent.record("s", generation, "a" * 64, None, "qq-image",
                      "[图片] material_id=history-image，mime_type=image/png", ("history-image",))

    await service.generate_queued("s", "刚才图片叫什么", request_id="text")

    call = client.calls[-1]
    assert call["model"] == "text-only"
    assert call["image_urls"] == () and call.get("image_material_ids", ()) == ()
    assert call["video_urls"] == ()
    assert call["messages"][0].content == "[图片] material_id=history-image，mime_type=image/png"

    await service.generate_queued("s", "识别本次图片", request_id="vision",
                                  image_material_ids=("current-image",))
    assert client.calls[-1]["model"] == "current-image-vision"
    assert client.calls[-1]["image_material_ids"] == ("current-image",)


async def test_in_memory_store_without_journal_preserves_normal_history():
    """未配置持久日志时，现有内存会话仍可读取配对历史和固定代际。"""
    client = RecordingModelClient()
    memory = ConversationStore(history_turns=10, max_sessions=10, ttl_seconds=3600)
    service = make_service(client, memory)
    try:
        generation = await service.history_generation("s")
        await service.commit_queued("s", "原问题", "原回答", request_id="r1")
        assert (await service.conversation_context("s")).generation_id == generation
        await service.generate_queued("s", "下一问", request_id="r2")
        assert [(message.role, message.content) for message in client.calls[-1]["messages"]] == [
            ("user", "原问题"), ("assistant", "原回答"), ("user", "下一问"),
        ]
    finally:
        await service.aclose()


@pytest.mark.parametrize("history_budget", [1000, 1])
async def test_long_file_model_view_respects_small_history_budget_without_losing_durable_content(
    service_context, history_budget,
):
    """预览能容纳完整素材引用时截断正文，过小时整条移除而原件日志保持完整。"""
    service, client, memory, sent = service_context
    memory.update_policy(ConversationMemoryPolicy(
        history_turns=10, max_history_chars=history_budget, ttl_seconds=3600,
        memory_enabled=False, max_memory_chars=1000,
    ))
    generation = await service.history_generation("s")
    material_id = "f" * 32
    original = "文件正文" * 500 + f"\n[文件 素材ID={material_id} 名称=完整输出.txt]"
    await sent.record("s", generation, "a" * 64, None, "qq-file", original, (material_id,))

    await service.generate_queued("s", "继续说明", request_id="current")

    messages = client.calls[-1]["messages"]
    assert messages[-1] == ChatMessage(role="user", content="继续说明")
    if history_budget == 1000:
        assert len(messages) == 2 and messages[0].role == "assistant"
        assert 0 < len(messages[0].content) <= 1000
        assert messages[0].content.startswith("文件正文" * 20)
        assert material_id in messages[0].content and "material_get" in messages[0].content
    else:
        assert messages == (ChatMessage(role="user", content="继续说明"),)
    rows = await sent.list_messages("s", generation)
    assert len(rows) == 1 and rows[0].content == original
    assert rows[0].material_ids == (material_id,)
