"""素材读取、主动回复、独立图片投递和持久上下文的离线贯通。"""

import asyncio
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.agent.contracts import ToolCall
from agent_bot.agent.tools.materials import MaterialGetTool
from agent_bot.config import RuntimeConfig
from agent_bot.integrations.onebot.outgoing import DeliveryScope, OutgoingRecorder
from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
from agent_bot.integrations.onebot.replies import OneBotReplyGateway
from agent_bot.integrations.onebot.tooling import ChatToolSessionLease
from agent_bot.integrations.sqlite_queue_database import SqliteQueueBusyError
from agent_bot.integrations.sqlite_tool_operations import SqliteToolOperationStore
from agent_bot.materials.contracts import MaterialSearchArguments
from agent_bot.media.contracts import MediaOwner, MediaScope
from agent_bot.memory_store import ConversationMemoryPolicy, MySqlConversationMemoryStore
from agent_bot.model_router import ContentModelRouter
from agent_bot.sent_messages import SentMessageStore
from agent_bot.services.chat import ChatService
from agent_bot.services.conversation_queue import (
    ConversationJob,
    ConversationResult,
    PersistedGeneratedReply,
)
from agent_bot.services.replies import GeneratedReply
from materials.test_store import material_database as _legacy_material_database
from materials.test_store import materials as materials
from media.test_inputs import picture

legacy_material_database = _legacy_material_database


@pytest.fixture
def material_database(legacy_material_database):
    """旧轮次与出站适配器的兼容测试显式加载归档结构，新库不创建旧表。"""
    params, _ = legacy_material_database
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.executescript(Path('sql/legacy/session-context-v1.sqlite.sql').read_text('utf-8'))
    return legacy_material_database


def make_pipeline_processor(service, config, recorder, gateway, bot):
    """复用真实队列提交编排，运行时租约和 QQ 网络保持离线。"""
    @asynccontextmanager
    async def lease(*args):
        """保持生产租约边界，不引入工具或网络依赖。"""
        yield ChatToolSessionLease(tools=(), token=None)

    return ConversationQueueProcessor(
        config_provider=lambda: config, chat_service_provider=lambda: service,
        runtime_lease=lease, tool_session_provider=lease, bot_provider=lambda _: bot,
        reply_composer=None, reply_gateway=gateway,
        operation_store_provider=lambda: None, image_submitter=AsyncMock(),
        workflow_service_provider=lambda: None, outgoing_recorder=recorder,
    )


@pytest.mark.parametrize("proactive", [False, True])
async def test_material_picture_is_sent_separately_and_in_next_chat_context(
    material_database, materials, proactive,
):
    """真实读取器与队列打通后，主动消息也能独立发图，后续聊天仍只使用聊天模型。"""
    params, pool = material_database
    owner = MediaOwner(entry="qq", session_id="s")
    asset = await materials.put(
        owner=owner, name="上一轮.png", mime_type="image/png", data=picture(),
    )
    read = MaterialGetTool(materials, MediaScope(
        owner=owner, execution_id="b" * 32, execution_kind="agent",
    ))
    client = SimpleNamespace(complete=AsyncMock())

    async def model(**kwargs):
        """只替换模型响应，图片从真实素材工具进入独立附件收集器。"""
        assert kwargs['model'] == 'chat'
        await read.call(ToolCall(call_id='get', name='material_get', arguments={'id': asset.id}))
        return '给你看看这张图片。'

    client.complete.side_effect = model
    memory = MySqlConversationMemoryStore(
        params, connect=pool.connect, policy=ConversationMemoryPolicy(
            history_turns=10, max_history_chars=10000, ttl_seconds=3600,
            memory_enabled=False, max_memory_chars=4000,
        ),
    )
    sent = SentMessageStore(params, connect=pool.connect)
    service = ChatService(
        client=client, store=memory, sent_messages=sent, system_prompt='当前角色',
        max_input_chars=1000, cooldown_seconds=0, max_concurrent_requests=2,
        model_router=ContentModelRouter('chat', 'vision', 'image', 'audio'),
    )

    async def event_scope(request_id):
        """没有真实事件时由测试显式提供原聊天身份。"""
        return DeliveryScope('s', await service.history_generation('s'), request_id)

    recorder = OutgoingRecorder(service_provider=lambda: service, store_provider=lambda: sent,
                                materials_provider=lambda: materials, scope_provider=event_scope)
    config = RuntimeConfig(onebot_v11_access_token='x' * 40, chat_session_secret='y' * 40)
    gateway = OneBotReplyGateway(lambda: config, recorder=recorder)
    bot = SimpleNamespace(send_group_msg=AsyncMock(
        side_effect=[{'message_id': 1}, {'message_id': 2}],
    ))

    processor = make_pipeline_processor(service, config, recorder, gateway, bot)
    job = ConversationJob(job_id='proactive' if proactive else 'ordinary', request_id='request',
                          created_at=time.time(), session_id='s', bot_id='90001', scene='group',
                          group_id='67890', user_id='12345', prompt='接着聊',
                          auto_speak_max_reply_chars=300 if proactive else None)
    result = await processor.generate(job)
    assert len(result.tool_images) == 1
    for index in range(await processor.delivery_count(job, result)):
        await processor.deliver_part(job, result, index)
    await processor.commit(job, result)
    calls = bot.send_group_msg.await_args_list
    assert [part.type for part in calls[0].kwargs['message']] == ['image']
    assert all(part.type != 'image' for part in calls[1].kwargs['message'])
    context = await service.conversation_context('s')
    text = '\n'.join(message.content for message in context.messages)
    assert asset.id in text and text.count('给你看看这张图片。') == 1
    assert 'base64' not in text
    if proactive:
        assert all(message.role == 'assistant' for message in context.messages)
    else:
        assert any(message.role == 'user' for message in context.messages)
    assert await materials.describe(asset.id, owner=owner) == asset

    client.complete.side_effect = None
    client.complete.return_value = '上一轮发了一张图片。'
    await service.ask('s', '上一轮发了什么')
    call = client.complete.await_args.kwargs
    assert call['model'] == 'chat' and not call['image_urls']
    assert any(asset.id in message.content for message in call['messages'])


@pytest.fixture
def live_confirmation_receipts(tmp_path):
    """独立真实队列库只保存测试确认回执，不使用应用部署文件。"""
    path = tmp_path / 'live-confirmation.sqlite3'
    with sqlite3.connect(path) as connection:
        connection.executescript(
            Path('sql/agent_bot_queue_init.sqlite.sql').read_text(encoding='utf-8'),
        )
    return SqliteToolOperationStore(str(path), key_prefix='live-confirmation', ttl_seconds=60)


async def test_next_context_recovers_live_platform_confirmation_without_replaying_send(
    material_database, materials, live_confirmation_receipts, monkeypatch,
):
    """事件投递成功而回执库忙时，下次读取真实上下文即补记成功，无需重放事件。"""
    params, pool = material_database
    receipts = live_confirmation_receipts
    memory = MySqlConversationMemoryStore(
        params, connect=pool.connect, policy=ConversationMemoryPolicy(
            history_turns=10, max_history_chars=10000, ttl_seconds=3600,
            memory_enabled=False, max_memory_chars=4000,
        ),
    )
    sent = SentMessageStore(
        params, connect=pool.connect, receipts_provider=lambda: receipts,
        live_receipts_provider=lambda: recorder.confirmed_receipts,
    )
    client = SimpleNamespace(complete=AsyncMock())
    service = ChatService(
        client=client, store=memory, sent_messages=sent, system_prompt='',
        max_input_chars=1000, cooldown_seconds=0, max_concurrent_requests=2,
        model_router=ContentModelRouter('chat', 'vision', 'image', 'audio'),
    )

    async def event_scope(request_id):
        """事件只从真实历史存储取得当前代际，模型不能指定记录身份。"""
        return DeliveryScope('live-session', await service.history_generation('live-session'),
                             request_id)

    recorder = OutgoingRecorder(
        service_provider=lambda: service, store_provider=lambda: sent,
        materials_provider=lambda: materials, scope_provider=event_scope,
        receipts_provider=lambda: receipts,
    )
    config = RuntimeConfig(onebot_v11_access_token='x' * 40, chat_session_secret='y' * 40)
    gateway = OneBotReplyGateway(lambda: config, recorder=recorder)
    bot = SimpleNamespace(send_private_msg=AsyncMock(return_value={'message_id': 88}))
    real_complete = receipts.complete
    fail_once = True

    async def complete_after_busy(job_id, tool, signature, effect):
        """只注入首次确认事务写入失败，真实读取与其余事务保持原实现。"""
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise SqliteQueueBusyError('本地队列数据库忙')
        await real_complete(job_id, tool, signature, effect)

    monkeypatch.setattr(receipts, 'complete', complete_after_busy)
    reply = GeneratedReply(text='事件通知已实际送达', requestId='live-confirmation-event')
    try:
        with pytest.raises(SqliteQueueBusyError):
            await gateway.send_bot_exact_part(bot, scene='private', target_id='12345', reply=reply)
        bot.send_private_msg.assert_awaited_once()
        assert len(recorder.confirmed_receipts) == 1
        delivery_id = next(iter(recorder.confirmed_receipts))
        assert await receipts.completed(delivery_id, 'outgoing_message') is None

        # 不再次调用网关；上下文读取必须直接利用仍存活的真实平台确认。
        context = await service.conversation_context('live-session')
        assert [(message.role, message.content) for message in context.messages] == [
            ('assistant', '事件通知已实际送达'),
        ]
        generation = await service.history_generation('live-session')
        rows = await sent.list_messages('live-session', generation)
        assert len(rows) == 1 and rows[0].delivery_id == delivery_id
        assert rows[0].message_id == '88' and rows[0].content == reply.text
        assert recorder.confirmed_receipts == {}
        bot.send_private_msg.assert_awaited_once()
        client.complete.assert_not_awaited()
    finally:
        await service.aclose()


async def test_long_utf8_file_keeps_full_original_and_sent_log_with_bounded_chat_context(
    material_database, materials,
):
    """超过旧历史字节上限的文件仍保存用户轮次，完整原件与有界模型预览独立保留。"""
    params, pool = material_database
    text = '😀' * 300000
    data = text.encode('utf-8')
    assert len(data) > 1048576
    memory = MySqlConversationMemoryStore(
        params, connect=pool.connect, policy=ConversationMemoryPolicy(
            history_turns=10, max_history_chars=12000, ttl_seconds=3600,
            memory_enabled=False, max_memory_chars=4000,
        ),
    )
    sent = SentMessageStore(params, connect=pool.connect)
    client = SimpleNamespace(complete=AsyncMock(return_value='已读取上轮预览'))
    service = ChatService(
        client=client, store=memory, sent_messages=sent, system_prompt='',
        max_input_chars=1000, cooldown_seconds=0, max_concurrent_requests=2,
        model_router=ContentModelRouter('chat', 'vision', 'image', 'audio'),
    )

    async def event_scope(request_id):
        """文件归属固定为真实会话，日志与后续轮次使用同一代际。"""
        return DeliveryScope('long-file', await service.history_generation('long-file'), request_id)

    recorder = OutgoingRecorder(
        service_provider=lambda: service, store_provider=lambda: sent,
        materials_provider=lambda: materials, scope_provider=event_scope,
    )
    config = RuntimeConfig(onebot_v11_access_token='x' * 40, chat_session_secret='y' * 40)
    gateway = OneBotReplyGateway(lambda: config, recorder=recorder)
    bot = SimpleNamespace(call_api=AsyncMock(return_value={'file_id': 'uploaded-long-file'}))
    processor = make_pipeline_processor(service, config, recorder, gateway, bot)
    job = ConversationJob(
        job_id='long-file', request_id='long-file-request', created_at=time.time(),
        session_id='long-file', bot_id='90001', scene='private', user_id='12345',
        prompt='请导出完整文件',
    )
    reply = GeneratedReply(text=text, requestId=job.request_id)
    result = ConversationResult(
        kind='TEXT', reply_as_file=True, delivery_parts=(text,),
        reply=PersistedGeneratedReply(text=text, requestId=job.request_id),
    )
    try:
        await gateway.send_reply_file(bot, scene='private', target_id='12345', reply=reply)
        await processor.commit(job, result)
        bot.call_api.assert_awaited_once()
        raw_context = await asyncio.to_thread(memory.context, job.session_id)
        assert raw_context.messages[0].role == 'user'
        assert raw_context.messages[0].content == job.prompt
        assert len(raw_context.messages[1].content) < 33000

        owner = MediaOwner(entry='qq', session_id=job.session_id)
        assets = await materials.search(owner, MaterialSearchArguments(kind='file'))
        assert len(assets.records) == 1
        asset = assets.records[0]
        async with materials.open_content(asset.id, owner=owner) as file:
            original = await asyncio.to_thread(file.read)
        assert original == data and asset.size_bytes == len(data)
        generation = await service.history_generation(job.session_id)
        rows = await sent.list_messages(job.session_id, generation)
        assert len(rows) == 1
        assert rows[0].content.startswith(text + '\n')
        assert rows[0].material_ids == (asset.id,) and asset.id in rows[0].content

        context = await service.conversation_context(job.session_id)
        assert [message.role for message in context.messages] == ['user', 'assistant']
        assert all(len(message.content) <= 32000 for message in context.messages)
        assert asset.id in context.messages[1].content
        await service.generate_queued(
            job.session_id, '根据刚才文件继续', request_id='long-file-next',
        )
        model_messages = client.complete.await_args.kwargs['messages']
        assert all(len(message.content) <= 32000 for message in model_messages)
        assert any(asset.id in message.content for message in model_messages)
    finally:
        await service.aclose()
