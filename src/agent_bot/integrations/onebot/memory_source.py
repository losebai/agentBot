"""统一真人队列的证据身份，自动任务和派生产物不能冒充本人发言。"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from nonebot.adapters.onebot.v11 import MessageEvent

from agent_bot.memory.contracts import QuotedMemorySource
from agent_bot.services.tagged_memory import memory_scope_owner
from agent_bot.state import ConversationSourceBinding, current_conversation_source

if TYPE_CHECKING:
    from agent_bot.services.conversation_queue import ConversationJob


def quoted_memory_source(
    bot_id: str, event: MessageEvent, text: str | None,
) -> QuotedMemorySource | None:
    """仅从平台解析的真实引用绑定消息和外层作者，不信任正文内的身份声明。"""
    reply = getattr(event, "reply", None)
    if reply is None or not text:
        return None
    return QuotedMemorySource(
        text=text, event_id=f"qq:{bot_id}:{reply.message_id}",
        user_id=str(reply.sender.user_id) if reply.sender.user_id is not None else None,
        time=datetime.fromtimestamp(reply.time, UTC),
    )


def has_user_memory_source(job: ConversationJob) -> bool:
    """只允许真人入站 QQ 请求成为用户证据，后台绘图等结果沿用原授权但不新增原话。"""
    return (
        job.request_id.startswith("qq-") and job.user_id != job.bot_id
        and not job.background_image and job.ai_job_id is None
        and job.auto_speak_max_reply_chars is None
        and job.memory_source_text is not None
    )


def queued_source_event_id(job: ConversationJob) -> str | None:
    """使用服务端持久任务身份作来源引用，不能谎称队列ID是QQ平台消息ID。"""
    return f"queue:{job.bot_id}:{job.job_id}" if has_user_memory_source(job) else None


def queued_memory_source(job: ConversationJob, secret: str) -> dict[str, str] | None:
    """接收摘要与长期记忆使用同一HMAC主体和来源引用，恢复任务也不改变身份。"""
    event_id = queued_source_event_id(job)
    if event_id is None:
        return None
    source = {
        "speaker_id": memory_scope_owner(job.bot_id, "user", job.user_id, secret),
        "scene": job.scene, "source_kind": "user", "platform": "qq",
        "bot_id": job.bot_id, "source_event_id": event_id,
        "source_text": job.memory_source_text,
    }
    if job.group_id is not None:
        source["group_id"] = job.group_id
    return source


def queued_evolution_source(
    job: ConversationJob, secret: str,
) -> ConversationSourceBinding | None:
    """演化只接受平台原始消息；任务重入队与延迟不改变证据身份或日期。"""
    if job.source_message_id is None or job.source_message_time is None:
        return None
    source = queued_memory_source(job, secret)
    if source is None:
        return None
    # 旧记忆的 queue 引用保持兼容，演化单独以平台消息去重。
    source['source_event_id'] = f'qq:{job.bot_id}:{job.source_message_id}'
    return ConversationSourceBinding(
        job.session_id, source, datetime.fromtimestamp(job.source_message_time, UTC),
    )


@contextmanager
def direct_memory_source(
    *, session_id: str, bot_id: str, user_id: str, message_id: int,
    created_at: float, secret: str, group_id: str | None = None,
    source_text: str,
) -> Iterator[None]:
    """真实同步事件租约向聊天服务传递平台证据，退出或取消后复原原上下文。"""
    source = {
        "speaker_id": memory_scope_owner(bot_id, "user", user_id, secret),
        "scene": "group" if group_id is not None else "private", "source_kind": "user",
        "platform": "qq", "bot_id": bot_id, "source_event_id": f"qq:{bot_id}:{message_id}",
        "source_text": source_text,
    }
    if group_id is not None:
        source["group_id"] = group_id
    token = current_conversation_source.set(ConversationSourceBinding(
        session_id, source, datetime.fromtimestamp(created_at, UTC),
    ))
    try:
        yield
    finally:
        current_conversation_source.reset(token)
