"""QQ 回复清除空白行，同时保留单个换行、正文缩进和队列分段一致性。"""

from unittest.mock import AsyncMock, Mock

import pytest
from test_onebot_replies import FakeBot, FakeMatcher, gateway

from agent_bot.integrations.onebot.queue_processor import generated_reply_from_model
from agent_bot.services.conversation_queue import ConversationJob, PersistedGeneratedReply
from agent_bot.services.replies import GeneratedReply


@pytest.mark.parametrize('raw, expected', [
    ('第一段\n\n第二段\n\n\n第三段', '第一段\n第二段\n第三段'),
    ('第一行\r\n \t\r\n第二行\r\r第三行', '第一行\n第二行\n第三行'),
    ('列表：\n\n  - 第一项\n\u3000\n  - 第二项', '列表：\n  - 第一项\n  - 第二项'),
    ('正常\n换行', '正常\n换行'),
    ('分段末尾\n', '分段末尾\n'),
])
def test_generated_reply_has_no_blank_lines(raw: str, expected: str) -> None:
    """回复入口清除连续或仅含空格的空白行，单个分段边界仍保留。"""
    reply = GeneratedReply(text=raw, requestId='spacing')
    assert reply.text == expected
    assert GeneratedReply.model_validate(reply.model_dump()).text == expected


@pytest.mark.parametrize('mode', ['bot', 'exact', 'tools', 'matcher'])
async def test_every_text_delivery_path_has_no_double_newlines(mode: str) -> None:
    """普通分段、队列单段、工具增强及 Matcher 输出遵循同一换行规则。"""
    bot, matcher, sender = FakeBot(), FakeMatcher(), gateway(chunk_chars=100)
    reply = GeneratedReply(text='第一段\n\n第二段\n \n第三段', requestId='spacing')
    if mode == 'matcher':
        await sender.send_matcher(matcher, reply)
        messages = matcher.sent
    else:
        method = {
            'bot': sender.send_bot,
            'exact': sender.send_bot_exact_part,
            'tools': sender.send_tool_reply,
        }[mode]
        await method(bot, scene='private', target_id='10001', reply=reply)
        messages = [call.message for call in bot.calls]
    assert [message.extract_plain_text() for message in messages] == ['第一段\n第二段\n第三段']


async def test_normalized_parts_keep_delivery_receipt_consistent() -> None:
    """归一化在分段前完成，最终可见文字和送达回执保持一致。"""
    reply = generated_reply_from_model('\n第一段\n\n第二段\n\n第三段\n', 'spacing')
    bot = FakeBot()
    receipt = await gateway(chunk_chars=5).send_bot(
        bot, scene='private', target_id='10001', reply=reply,
    )
    texts = [call.message.extract_plain_text() for call in bot.calls]
    assert ''.join(texts) == receipt.text == reply.text == '第一段\n第二段\n第三段'
    assert all('\n\n' not in text for text in texts)


async def test_scheduled_reply_normalizes_before_persisting_parts(chat_plugin, monkeypatch) -> None:
    """已生成任务正文也先规范化，持久化正文和分段不能产生不一致。"""
    service = Mock(generate_queued=AsyncMock())
    monkeypatch.setattr(chat_plugin, 'chat_service', service)
    request_id = 'ai-job-' + 'a' * 32
    job = ConversationJob(
        jobId=request_id, requestId=request_id, createdAt=1_800_000_000,
        sessionId='session-a', botId='90001', scene='group', groupId='20001',
        userId='10001', prompt='任务', aiJobId='a' * 32,
        aiJobReply=PersistedGeneratedReply(text='任务正文\n\n第二段', requestId=request_id),
    )
    result = await chat_plugin._ConversationProcessor().generate(job)
    assert result.reply.text == ''.join(result.delivery_parts) == '任务正文\n第二段'
    service.generate_queued.assert_not_awaited()
