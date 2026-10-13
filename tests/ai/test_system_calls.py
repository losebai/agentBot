"""系统调用在分页前分类，指令上下文不得污染并发普通对话。"""

import asyncio
from datetime import datetime

import pytest

from agent_bot.ai.call_origin import command_call, command_scope
from agent_bot.ai.call_records import CallRecordStore, legacy_record
from agent_bot.ai.contracts import SessionWrite, TokenUsage
from agent_bot.ai.errors import AiError
from agent_bot.ai.sessions import AiSessionStore


async def test_system_group_filters_before_paging_and_binds_cursor(ai_params, ai_database):
    """混合记录按分组稳定分页，类型筛选和详情使用相同分类。"""
    store = CallRecordStore(ai_params, connect=ai_database.connect)
    rows = [
        {"id": 1, "data": {"choices": []}},
        {"id": 2, "data": {"type": "auto_speak_decision"}},
        {"id": 3, "data": {"choices": [], "system_kind": "command"}},
        {"id": 4, "request_id": "qq-summary-history", "data": {"choices": []}},
        {"id": 5, "request_id": "qq-reply-history", "content": "/查询 玩家"},
        {"id": 6, "request_id": "qq-reply-normal", "content": "你好"},
        {"id": 7, "session_Id": "group:quoted-summary"},
        {"id": 8, "request_id": "qq-draw-history"},
    ]
    await store.import_batch([legacy_record(row, "history") for row in rows])
    ordinary = await store.list_calls(category="conversation")
    assert {row.source_id for row in ordinary.items} == {"1", "6"}
    first = await store.list_calls(category="system", limit=2)
    assert first.next_cursor
    second = await store.list_calls(category="system", limit=2, cursor=first.next_cursor)
    assert not {row.id for row in first.items} & {row.id for row in second.items}
    for filters in ({"category": "conversation"}, {"category": "system", "system_kind": "command"}):
        with pytest.raises(AiError):
            await store.list_calls(**filters, cursor=first.next_cursor)
    commands = await store.list_calls(category="system", system_kind="command")
    assert {row.source_id for row in commands.items} == {"3", "4", "5", "7", "8"}
    detail = await store.get_call(commands.items[0].id)
    assert detail.system_kind == "command"
    assert "data" not in commands.items[0].model_dump()
    decisions = await store.list_calls(category="system", system_kind="auto_speak")
    assert [row.source_id for row in decisions.items] == ["2"]


async def test_command_context_is_task_local_and_reaches_database_worker(ai_params, ai_database):
    """显式指令中的模型写入带标记，另一协程和异常后的普通调用不受影响。"""
    sessions = AiSessionStore(ai_params, connect=ai_database.connect)
    started, release = asyncio.Event(), asyncio.Event()

    async def save(key):
        """真实落入内存事务，校验线程边界仍携带当前任务分类。"""
        await sessions.save(SessionWrite(
            execution_id=key * 32, session_id="s", request_id=key,
            configured_model="alias", model="model", status="completed", duration_ms=1,
            usage=TokenUsage(), created_at=datetime.now(), prompt="请求", content="回复",
        ))

    async def run_command():
        """取消或异常都必须还原任务局部标记。"""
        with pytest.raises(RuntimeError):
            async with command_scope():
                started.set()
                await release.wait()
                await save("a")
                raise RuntimeError("离线异常")
        assert not command_call.get()

    task = asyncio.create_task(run_command())
    await started.wait()
    await save("b")
    release.set()
    await task
    store = CallRecordStore(ai_params, connect=ai_database.connect)
    assert [r.request_id for r in (await store.list_calls(category="system")).items] == ["a"]
    assert [r.request_id for r in (await store.list_calls(category="conversation")).items] == ["b"]


async def test_old_system_markers_excluded_even_with_ordinary_type(ai_params, ai_database):
    """历史记录类型未更新时，查询仍按已有决策标记排除，且分页不漏普通记录。"""
    store = CallRecordStore(ai_params, connect=ai_database.connect)
    raw = [
        {"id": 1, "data": {"choices": []}},
        {"id": 2, "data": {"type": "auto_speak_decision"}},
        {"id": 3, "request_id": "auto-decision-old"},
        {"id": 4, "data": {"system_kind": "auto_speak"}},
        {"id": 5, "request_context": {"state": "旧状态"}, "data": {"shouldSpeak": False}},
        {"id": 6, "data": {"original_data": {"system_kind": "command"}}},
        {"id": 7, "data": {"choices": []}},
        {"id": 8},
    ]
    rows = [{**legacy_record(row, "history"), "type": "ordinary"} for row in raw]
    rows[-1]["source"] = "python:agent_bot_auto_speak_decision"
    await store.import_batch(rows)
    first = await store.list_calls(category="conversation", limit=1)
    second = await store.list_calls(category="conversation", limit=1, cursor=first.next_cursor)
    assert {row.source_id for row in first.items + second.items} == {"1", "7"}
    assert second.next_cursor is None
    system = await store.list_calls(category="system")
    assert {row.source_id for row in system.items} == {"2", "3", "4", "5", "6", "8"}
    for item in system.items:
        assert (await store.get_call(item.id)).system_kind == item.system_kind
