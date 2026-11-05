"""独立Agent的本地身份、持久对话和研究不依赖QQ账户或真实模型。"""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from agent_bot.ai.errors import AiError
from agent_bot.autonomy.contracts import Identity, LocalOwner, Policy, ProfileCreate, ProfileUpdate
from agent_bot.autonomy.service import AutonomyService, memory_identity
from agent_bot.autonomy.store import AutonomyStore


def local_body(**policy):
    """本地参与者完全使用非平台标识，预算由测试显式允许。"""
    return ProfileCreate(owner=LocalOwner(agent_key='agent-alice', participant_id='human',
                                         session_id='local-session'),
                         identity=Identity(name='Alice'), policy=Policy(**policy))


async def test_local_messages_resume_in_order_and_keep_context_without_qq(store, memories):
    """本地消息与任务原子幂等，重启保留前文并把结果保存在独立对话中。"""
    profile = await store.create_profile(local_body(
        paused=False, allowed_actions=('draft',), model='model:1',
        input_cost_per_million=1, output_cost_per_million=1, daily_cost_limit=1))
    id = profile['id']
    first = await store.send_message(id, 'message-1', '我叫小白')
    assert (await store.send_message(id, 'message-1', '我叫小白'))['id'] == first['id']
    with pytest.raises(AiError, match='AUTONOMY_CONFLICT'):
        await store.send_message(id, 'message-1', '覆盖原文')
    second = await store.send_message(id, 'message-2', '我叫什么？')
    ai = SimpleNamespace(complete=AsyncMock(side_effect=[
        SimpleNamespace(content='你好，小白。'), SimpleNamespace(content='你叫小白。')]))
    restored = AutonomyStore(store.params, connect=store.connect, clock=store.clock,
                             secret='offline-autonomy-secret')
    service = AutonomyService(restored, memories, ai)
    assert await service.run_once(id)
    assert await service.run_once(id)
    data = json.loads(ai.complete.await_args_list[1].args[0].prompt)
    assert data['conversation'] == [{'role': 'user', 'content': '我叫小白'},
                                    {'role': 'assistant', 'content': '你好，小白。'}]
    assert (await store.tasks(id))[0]['status'] == 'succeeded'
    assert any(row['task'] and row['task']['id'] == second['id']
               and row['task']['outputs'] == ['你叫小白。']
               for row in (await restored.conversations(id))['items'])
    def count(connection):
        """本地回复不得创建任何QQ出站任务。"""
        from agent_bot.ai.database import query
        return query(connection, 'SELECT COUNT(*) AS count FROM agent_bot_job', one=True)['count']
    assert await store.run(count) == 0
    identity = memory_identity(profile['owner'])
    assert identity.bot_id == 'local-agent:agent-alice' and identity.user_id == 'human'


async def test_local_pause_is_queue_boundary_and_qq_policy_is_rejected(store):
    """暂停可保存消息但不能领取；外部出站开关不能通过更新绕过本地边界。"""
    profile = await store.create_profile(local_body())
    id = profile['id']
    await store.send_message(id, 'paused', '暂停时收到的消息')
    assert await store.claim(id) is None
    with pytest.raises(AiError, match='AUTONOMY_LOCAL_CHANNEL_REQUIRED'):
        await store.update_profile(id, ProfileUpdate(expected_revision=1,
                                   policy=Policy(notify_qq=True)))
    assert (await store.get_profile(id))['policy']['notify_qq'] is False
    stranger = AutonomyStore(store.params, connect=store.connect, secret='other-secret')
    with pytest.raises(AiError, match='AUTONOMY_NOT_FOUND'):
        await stranger.send_message(id, 'bad', '不可读取别人的会话')
