"""统一发送边界只登记真实投递，媒体与文件保存原件并关联素材。"""

import base64
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import MessageSegment

from agent_bot.ai.chat_errors import ChatAiUnavailableError
from agent_bot.ai.errors import AiError
from agent_bot.integrations.onebot.outgoing import DeliveryScope, OutgoingRecorder
from agent_bot.integrations.onebot.replies import OneBotReplyGateway
from agent_bot.integrations.sqlite_queue_database import SqliteQueueBusyError
from agent_bot.integrations.sqlite_tool_operations import SqliteToolOperationStore
from agent_bot.services.replies import GeneratedReply
from agent_bot.services.tool_operations import DeliveryEffect
from media.test_inputs import picture


class Journal:
    """记录本地提交顺序，替代数据库并保留未知投递状态。"""

    def __init__(self):
        """每个测试使用独立的投递账本。"""
        self.rows = {}
        self.sent = []

    async def claim_delivery(self, **values):
        """先记录待投递；重试已完成消息返回回执，未知结果拒绝重发。"""
        key = values['delivery_id']
        if key in self.rows:
            if self.rows[key].message_id is None:
                raise RuntimeError('SENT_MESSAGE_DELIVERY_UNCERTAIN')
            return self.rows[key]
        self.rows[key] = SimpleNamespace(**values)
        return None

    async def finish_delivery(self, delivery_id, message_id, *, session_id):
        """只有平台确认后才能把消息计入成功上下文。"""
        row = self.rows[delivery_id]
        assert row.session_id == session_id
        row.message_id = message_id
        self.sent.append(row)
        return row


def setup_recorder(*, journal=None, receipts=None):
    """提供真实网关与上下文登记器，仅替换持久化和 OneBot 网络。"""
    journal = Journal() if journal is None else journal
    materials = SimpleNamespace(
        put=AsyncMock(return_value=SimpleNamespace(
            id='a' * 32, name='原件', kind='image', mime_type='image/png', size_bytes=80,
        )),
        add_category=AsyncMock(),
    )
    service = SimpleNamespace(history_generation=AsyncMock(return_value='generation-one'))

    async def scope(request_id):
        """事件身份由宿主解析，消息正文不能指定所属会话。"""
        return DeliveryScope('s', 'generation-one', request_id or 'event-one')

    recorder = OutgoingRecorder(
        service_provider=lambda: service, store_provider=lambda: journal,
        materials_provider=lambda: materials, scope_provider=scope,
        receipts_provider=(lambda: receipts) if receipts is not None else None,
    )
    gateway = OneBotReplyGateway(
        lambda: SimpleNamespace(chat_chunk_chars=3, chat_max_reply_chars=100), recorder=recorder,
    )
    bot = SimpleNamespace(send_private_msg=AsyncMock(return_value={'message_id': 21}),
                          call_api=AsyncMock(return_value={'file_id': 'file-1'}))
    return recorder, gateway, bot, journal, materials, service


async def test_text_parts_are_recorded_once_after_platform_ack():
    """上下文保存实际可见分段；已确认投递的重复恢复不再调用平台。"""
    _, gateway, bot, journal, _, _ = setup_recorder()
    reply = GeneratedReply(text='abcdef', requestId='request-one')
    await gateway.send_bot(bot, scene='private', target_id='12345', reply=reply)
    assert [row.content for row in journal.sent] == ['abc', 'def']
    await gateway.send_bot(bot, scene='private', target_id='12345', reply=reply)
    assert bot.send_private_msg.await_count == 2


async def test_picture_is_archived_and_context_contains_only_material_reference():
    """图片独立投递，原件进入素材库，历史没有 Base64 或易失链接。"""
    _, gateway, bot, journal, materials, _ = setup_recorder()
    data = picture()
    await gateway.send_media_bot(
        bot, scene='private', target_id='12345',
        segment=MessageSegment.image('base64://' + base64.b64encode(data).decode()),
    )
    assert materials.put.await_args.kwargs['data'] == data
    assert journal.sent[0].material_ids == ('a' * 32,)
    assert 'a' * 32 in journal.sent[0].content
    assert 'base64' not in journal.sent[0].content
    assert [part.type for part in bot.send_private_msg.await_args.kwargs['message']] == ['image']


async def test_generated_file_keeps_original_bytes_and_reference():
    """文件正文和文件自身都可追溯，文件不依赖临时上传目录。"""
    _, gateway, bot, journal, materials, _ = setup_recorder()
    reply = GeneratedReply(text='完整文件内容', requestId='long-reply')
    await gateway.send_reply_file(bot, scene='private', target_id='12345', reply=reply)
    assert materials.put.await_args.kwargs['data'] == '完整文件内容'.encode()
    assert materials.put.await_args.kwargs['mime_type'] == 'text/plain'
    assert journal.sent[0].material_ids == ('a' * 32,)
    assert '完整文件内容' in journal.sent[0].content
    assert bot.call_api.await_args.args[0] == 'upload_private_file'


async def test_failed_or_uncertain_delivery_does_not_become_success_or_resend():
    """网络结果不明时保留待核对状态，不能把保存上下文失败变成二次发送。"""
    _, gateway, bot, journal, _, _ = setup_recorder()
    bot.send_private_msg.side_effect = ConnectionError('offline')
    reply = GeneratedReply(text='hello', requestId='r')
    with pytest.raises(ConnectionError):
        await gateway.send_bot_exact_part(bot, scene='private', target_id='12345', reply=reply)
    assert not journal.sent
    with pytest.raises(RuntimeError, match='UNCERTAIN'):
        await gateway.send_bot_exact_part(bot, scene='private', target_id='12345', reply=reply)
    bot.send_private_msg.assert_awaited_once()


async def test_background_delivery_keeps_original_generation_after_reset():
    """后台旧任务仍能投递，但登记原代际，不能把旧图片加入重置后的历史。"""
    recorder, gateway, bot, journal, _, service = setup_recorder()
    service.history_generation.return_value = 'new-generation'
    async with recorder.scope('s', 'background', key='image:0', generation_id='old-generation'):
        await gateway.send_bot_exact_part(
            bot, scene='private', target_id='12345',
            reply=GeneratedReply(text='已完成', requestId='background-completed'),
        )
    assert journal.sent[0].generation_id == 'old-generation'
    service.history_generation.assert_not_called()


@pytest.fixture
def outgoing_receipts(tmp_path):
    """用正式队列结构建立独立 SQLite，不连接部署的主库或队列。"""
    path = tmp_path / 'outgoing-receipts.sqlite3'
    schema = Path('sql/agent_bot_queue_init.sqlite.sql').read_text(encoding='utf-8')
    with sqlite3.connect(path) as connection:
        connection.executescript(schema)
    return SqliteToolOperationStore(str(path), key_prefix='outgoing-test', ttl_seconds=60)


class UnavailableOnceJournal(Journal):
    """只在第一次确认上下文时失败，平台发送和持久回执不受影响。"""

    def __init__(self):
        """失败不会提前改变待投递日志或伪造成功。"""
        super().__init__()
        self.unavailable = True

    async def finish_delivery(self, delivery_id, message_id, *, session_id):
        """模拟平台已确认、但业务数据库临时不可用的恢复窗口。"""
        if self.unavailable:
            self.unavailable = False
            raise AiError('AI_DATABASE_UNAVAILABLE', status_code=503)
        return await super().finish_delivery(delivery_id, message_id, session_id=session_id)


@pytest.mark.parametrize('rebuild_recorder', [False, True])
async def test_confirmed_delivery_recovers_journal_without_platform_resend(
    outgoing_receipts, rebuild_recorder,
):
    """日志提交失败后，同实例或重建实例都只补记上下文，不能再次调用 QQ。"""
    journal = UnavailableOnceJournal()
    _, gateway, bot, _, _, _ = setup_recorder(journal=journal, receipts=outgoing_receipts)
    reply = GeneratedReply(text='平台已接收的内容', requestId='confirmed-before-history-failure')
    with pytest.raises(ChatAiUnavailableError) as failure:
        await gateway.send_bot_exact_part(bot, scene='private', target_id='12345', reply=reply)
    assert failure.value.error_code == 'OUTGOING_HISTORY_UNAVAILABLE'
    assert not journal.sent
    bot.send_private_msg.assert_awaited_once()
    delivery_id = next(iter(journal.rows))

    # 新的存储对象没有内存缓存，仍可读取第一次平台成功后提交的真实回执。
    restored_receipts = SqliteToolOperationStore(
        outgoing_receipts.path, key_prefix='outgoing-test', ttl_seconds=60,
    )
    effect = await restored_receipts.completed(delivery_id, 'outgoing_message')
    assert isinstance(effect, DeliveryEffect) and effect.message_id == '21'
    if rebuild_recorder:
        _, gateway, _, _, _, _ = setup_recorder(journal=journal, receipts=restored_receipts)
    result = await gateway.send_bot_exact_part(
        bot, scene='private', target_id='12345', reply=reply,
    )
    assert str(result['message_id']) == '21'
    assert len(journal.sent) == 1
    assert journal.sent[0].content == '平台已接收的内容'
    assert journal.sent[0].message_id == '21'
    bot.send_private_msg.assert_awaited_once()


async def test_failed_receipt_write_recovers_from_live_confirmation_without_resend(
    outgoing_receipts, monkeypatch,
):
    """持久回执首次写入失败时保留内存确认，恢复提交后才把日志记为已发送。"""
    real_complete = outgoing_receipts.complete
    fail_once = True

    async def complete_after_busy(job_id, tool, signature, result):
        """仅注入首次写入锁失败；后续写入使用真实 SQLite 事务和类型校验。"""
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise SqliteQueueBusyError('本地队列数据库忙')
        await real_complete(job_id, tool, signature, result)

    monkeypatch.setattr(outgoing_receipts, 'complete', complete_after_busy)
    _, gateway, bot, journal, _, _ = setup_recorder(receipts=outgoing_receipts)
    reply = GeneratedReply(text='回执写入失败前已发送', requestId='receipt-write-failure')
    with pytest.raises(SqliteQueueBusyError):
        await gateway.send_bot_exact_part(bot, scene='private', target_id='12345', reply=reply)
    assert not journal.sent
    bot.send_private_msg.assert_awaited_once()
    delivery_id = next(iter(journal.rows))
    assert await outgoing_receipts.completed(delivery_id, 'outgoing_message') is None

    result = await gateway.send_bot_exact_part(
        bot, scene='private', target_id='12345', reply=reply,
    )
    assert str(result['message_id']) == '21'
    assert len(journal.sent) == 1 and journal.sent[0].message_id == '21'
    assert journal.sent[0].content == '回执写入失败前已发送'
    restored_receipts = SqliteToolOperationStore(
        outgoing_receipts.path, key_prefix='outgoing-test', ttl_seconds=60,
    )
    effect = await restored_receipts.completed(delivery_id, 'outgoing_message')
    assert isinstance(effect, DeliveryEffect) and effect.message_id == '21'
    bot.send_private_msg.assert_awaited_once()


@pytest.mark.parametrize('rebuild_recorder', [False, True])
async def test_confirmed_delivery_retry_after_generation_change_finishes_original_journal(
    outgoing_receipts, rebuild_recorder,
):
    """旧代际已发送但日志未确认时，换代际重试只完成原日志，不能再次发消息。"""
    journal = UnavailableOnceJournal()
    recorder, gateway, bot, _, _, _ = setup_recorder(
        journal=journal, receipts=outgoing_receipts,
    )
    reply = GeneratedReply(text='旧代际实际已经发送', requestId='retry-after-generation-change')
    with pytest.raises(ChatAiUnavailableError):
        async with recorder.scope('s', reply.request_id, key='reply',
                                  generation_id='old-generation'):
            await gateway.send_bot_exact_part(bot, scene='private', target_id='12345', reply=reply)
    assert not journal.sent and len(journal.rows) == 1
    original_delivery_id = next(iter(journal.rows))
    assert journal.rows[original_delivery_id].generation_id == 'old-generation'
    bot.send_private_msg.assert_awaited_once()

    if rebuild_recorder:
        restored_receipts = SqliteToolOperationStore(
            outgoing_receipts.path, key_prefix='outgoing-test', ttl_seconds=60,
        )
        recorder, gateway, _, _, _, _ = setup_recorder(
            journal=journal, receipts=restored_receipts,
        )
    async with recorder.scope('s', reply.request_id, key='reply', generation_id='new-generation'):
        result = await gateway.send_bot_exact_part(
            bot, scene='private', target_id='12345', reply=reply,
        )
    assert str(result['message_id']) == '21'
    assert list(journal.rows) == [original_delivery_id]
    assert len(journal.sent) == 1
    assert journal.sent[0].generation_id == 'old-generation'
    assert journal.sent[0].content == '旧代际实际已经发送'
    assert journal.sent[0].message_id == '21'
    bot.send_private_msg.assert_awaited_once()


async def test_stored_video_uses_existing_asset_without_material_copy():
    """视频已归档到媒体库；不能被素材 20 MiB 上限阻止，也不能重复发送。"""
    from datetime import datetime, timedelta

    from agent_bot.media.contracts import MediaAssetView

    _, gateway, bot, journal, materials, _ = setup_recorder()
    now = datetime.now()
    asset = MediaAssetView(id="b" * 32, purpose="output", mime_type="video/mp4",
        size_bytes=21 * 1024 * 1024, sha256="c" * 64, status="ready", created_at=now,
        expires_at=now + timedelta(days=1))
    for _ in range(2):
        await gateway.send_video_asset_bot(bot, scene="private", target_id="12345",
            segment=MessageSegment.video("base64://encoded-only-at-send"), asset=asset)
    bot.send_private_msg.assert_awaited_once()
    materials.put.assert_not_awaited()
    assert asset.id in journal.sent[0].content
    assert "base64" not in journal.sent[0].content
