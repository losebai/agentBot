from __future__ import annotations

import asyncio

import pytest
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.adapters.onebot.v11.event import Sender

from agent_bot.integrations.onebot.auto_speak_trigger import GroupAutoSpeakTrigger


def group_event(message: str, *, group_id: int = 20001, message_id: int = 1) -> GroupMessageEvent:
    """Build one offline group text event for trigger ordering tests."""

    content = Message(message)
    return GroupMessageEvent(
        time=1,
        self_id=90001,
        post_type="message",
        sub_type="normal",
        user_id=10001,
        message_type="group",
        message_id=message_id,
        message=content,
        original_message=content,
        raw_message=str(content),
        font=0,
        sender=Sender(user_id=10001, nickname="tester", role="member"),
        group_id=group_id,
        to_me=False,
    )


class FakeBot:
    self_id = "90001"


class RecordingContextStore:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.messages: dict[str, list[str]] = {}

    def append(self, group_id: str, content: str) -> None:
        self.messages.setdefault(group_id, []).append(content)

    def context(self, *, bot_id: str, group_id: str) -> str | None:
        self.order.append(f"context:{group_id}")
        messages = self.messages.get(group_id, [])
        return "\n".join(messages) if messages else None


class RecordingService:
    def __init__(
        self,
        order: list[str],
        *,
        accepted_groups: set[str],
        block_first: bool = False,
    ) -> None:
        self.order = order
        self.accepted_groups = accepted_groups
        self.snapshots: list[tuple[str, str | None]] = []
        self.block_first = block_first
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()

    def accepts_message(self, bot_id: str, group_id: str) -> bool:
        return bot_id == "90001" and group_id in self.accepted_groups

    async def handle_group_message(
        self,
        bot: FakeBot,
        group_id: str,
        recent_group_context: str | None,
    ) -> bool:
        self.order.append(f"service:{group_id}")
        self.snapshots.append((group_id, recent_group_context))
        if self.block_first and len(self.snapshots) == 1:
            self.first_started.set()
            await self.release_first.wait()
        return True


def make_trigger(
    *,
    store: RecordingContextStore,
    service: RecordingService,
    order: list[str],
) -> GroupAutoSpeakTrigger:
    """Create a coordinator whose recorder exposes ordering in one shared list."""

    async def record(bot: FakeBot, event: GroupMessageEvent) -> None:
        group_id = str(event.group_id)
        order.append(f"record:{group_id}:{event.message_id}")
        store.append(group_id, event.get_plaintext())

    return GroupAutoSpeakTrigger(
        service_provider=lambda: service,
        recent_context_store_provider=lambda: store,
        record_recent_group_text=record,
    )


@pytest.mark.asyncio
async def test_message_trigger_records_before_context_and_decision() -> None:
    """当前消息先记入快照，与近期消息一起传入决策，且只出现一次。"""
    order: list[str] = []
    store = RecordingContextStore(order)
    store.append("20001", "近期消息")
    service = RecordingService(order, accepted_groups={"20001"})
    trigger = make_trigger(store=store, service=service, order=order)

    await trigger.handle(FakeBot(), group_event("当前消息"))

    assert order == ["record:20001:1", "context:20001", "service:20001"]
    assert service.snapshots == [("20001", "近期消息\n当前消息")]


@pytest.mark.asyncio
async def test_message_trigger_serializes_same_group_and_preserves_each_snapshot() -> None:
    order: list[str] = []
    store = RecordingContextStore(order)
    service = RecordingService(order, accepted_groups={"20001"}, block_first=True)
    trigger = make_trigger(store=store, service=service, order=order)

    first = asyncio.create_task(trigger.handle(FakeBot(), group_event("第一条", message_id=1)))
    await asyncio.wait_for(service.first_started.wait(), timeout=1)
    second = asyncio.create_task(trigger.handle(FakeBot(), group_event("第二条", message_id=2)))
    await asyncio.sleep(0)

    assert order == ["record:20001:1", "context:20001", "service:20001"]

    service.release_first.set()
    await asyncio.gather(first, second)

    assert service.snapshots == [
        ("20001", "第一条"),
        ("20001", "第一条\n第二条"),
    ]


@pytest.mark.asyncio
async def test_message_trigger_uses_independent_group_locks_and_contexts() -> None:
    order: list[str] = []
    store = RecordingContextStore(order)
    service = RecordingService(
        order,
        accepted_groups={"20001", "20002"},
        block_first=True,
    )
    trigger = make_trigger(store=store, service=service, order=order)

    first = asyncio.create_task(trigger.handle(FakeBot(), group_event("甲群", group_id=20001)))
    await asyncio.wait_for(service.first_started.wait(), timeout=1)
    await trigger.handle(FakeBot(), group_event("乙群", group_id=20002))

    assert ("20002", "乙群") in service.snapshots
    service.release_first.set()
    await first
    assert store.messages == {"20001": ["甲群"], "20002": ["乙群"]}


@pytest.mark.asyncio
async def test_non_target_or_non_message_service_records_only() -> None:
    order: list[str] = []
    store = RecordingContextStore(order)
    service = RecordingService(order, accepted_groups=set())
    trigger = make_trigger(store=store, service=service, order=order)

    await trigger.handle(FakeBot(), group_event("只记录", group_id=29999))

    assert order == ["record:29999:1"]
    assert service.snapshots == []
