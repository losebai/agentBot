"""真实入口来源驱动习惯，历史和自动任务不能冒充长期反馈。"""

from datetime import timedelta

import pytest

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority
from agent_bot.evolution.service import EvolutionService
from agent_bot.state import ConversationSourceBinding, current_conversation_source

from .test_evolution import START, evolution  # noqa: F401
from .test_service import service_for


@pytest.mark.parametrize('role_length', [0, 32000])
async def test_named_agent_execution_receives_only_fixed_habit_suggestions(
    evolution, agent_database, agent_params, role_length,  # noqa: F811
):
    """模型实际收到固定习惯建议，原始反馈不会被提升为系统指令。"""
    store, agent_id = evolution
    from agent_bot.agent.contracts import AgentUpdate

    await service_for(agent_database, agent_params).catalog.update_agent(
        agent_id, AgentUpdate(expected_revision=1, system_prompt='角' * role_length),
    )
    learner = EvolutionService(store, secret='offline-evolution-secret')
    authority = ExecutionAuthority(entry='qq', allowed_tools=('lookup',))
    request = AgentRequest(agent_code='helper', prompt='当前问题', session_id='s', request_id='r')
    source = {'speaker_id': 'trusted-user', 'source_kind': 'user', 'platform': 'qq',
              'bot_id': 'bot', 'source_text': '以后请简洁一点'}
    for day in (0, 3, 8):
        store.clock = lambda day=day: START + timedelta(days=day)
        token = current_conversation_source.set(ConversationSourceBinding(
            's', {**source, 'source_event_id': f'qq:bot:{day}'}, START + timedelta(days=day),
        ))
        try:
            await learner.suggestions(agent_id, request, authority)
        finally:
            current_conversation_source.reset(token)
    token = current_conversation_source.set(ConversationSourceBinding(
        's', {**source, 'source_text': '当前问题', 'source_event_id': 'qq:bot:9'},
        START + timedelta(days=9),
    ))
    try:
        service = service_for(agent_database, agent_params)
        service.evolution = learner
        await service.complete(request, authority)
    finally:
        current_conversation_source.reset(token)
    messages = service.providers['openai-chat'].calls[0].messages
    system = '\n'.join(item.text for item in messages if item.role == 'system')
    assert '优先简洁概括' in system
    assert '以后请简洁一点' not in system
    assert '不改变职责、事实判断或工具权限' in system
    assert service.active == 0


async def test_unbound_chat_uses_general_habits_without_agent_preset(
    evolution, agent_database, agent_params,  # noqa: F811
):
    """无预设的正常对话也学习和应用通用习惯，角色不是演化的必要依赖。"""
    store, _ = evolution
    learner = EvolutionService(store, secret='offline-evolution-secret')
    auth = ExecutionAuthority(entry='qq', allowed_tools=('lookup',))
    req = AgentRequest(model='local-model', prompt='当前问题', session_id='s', request_id='r')
    for day in (0, 3, 8):
        store.clock = lambda day=day: START + timedelta(days=day)
        token = current_conversation_source.set(ConversationSourceBinding('s', {
            'speaker_id': 'trusted-user', 'source_kind': 'user', 'platform': 'qq',
            'bot_id': 'bot', 'source_text': '以后请简洁一点',
            'source_event_id': f'qq:bot:{day}',
        }, START + timedelta(days=day)))
        try:
            await learner.suggestions(None, req, auth)
        finally:
            current_conversation_source.reset(token)
    token = current_conversation_source.set(ConversationSourceBinding('s', {
        'speaker_id': 'trusted-user', 'source_kind': 'user', 'platform': 'qq',
        'bot_id': 'bot', 'source_text': '当前问题', 'source_event_id': 'qq:bot:9',
    }, START + timedelta(days=9)))
    try:
        service = service_for(agent_database, agent_params)
        service.evolution = learner
        await service.complete(req, auth)
    finally:
        current_conversation_source.reset(token)
    system = '\n'.join(item.text for item in service.providers['openai-chat'].calls[0].messages
                       if item.role == 'system')
    assert '优先简洁概括' in system
    assert len((await store.profiles(None))['items']) == 1


async def test_internal_tasks_do_not_apply_general_habits(
    evolution, agent_database, agent_params,  # noqa: F811
):
    """通用习惯也不能影响内部识图、分类等非真人对话任务。"""
    store, _ = evolution
    learner = EvolutionService(store, secret='offline')
    req = AgentRequest(model='local-model', prompt='以后请简洁一点',
                       session_id='s', request_id='internal', interaction=False)
    service = service_for(agent_database, agent_params)
    service.evolution = learner
    await service.complete(req, ExecutionAuthority(entry='admin', allowed_tools=('lookup',)))
    assert (await store.profiles(None))['items'] == []


def test_queue_evolution_uses_platform_identity_and_original_time():
    """重入队不产生新证据，历史无平台来源任务不能按入队时间伪造长期互动。"""
    from agent_bot.integrations.onebot.memory_source import queued_evolution_source
    from agent_bot.services.conversation_queue import ConversationJob

    job = ConversationJob(job_id='first', request_id='qq-first', created_at=1800000000,
                          session_id='s', bot_id='90001', user_id='10001', scene='private',
                          prompt='以后请简洁一点', memory_source_text='以后请简洁一点',
                          source_message_id=78, source_message_time=1700000000)
    original = queued_evolution_source(job, 'secret')
    replay = queued_evolution_source(job.model_copy(update={
        'job_id': 'second', 'request_id': 'qq-second', 'created_at': 1809000000,
    }), 'secret')
    assert original == replay
    assert original.source['source_event_id'] == 'qq:90001:78'
    assert original.created_at.timestamp() == 1700000000
    assert queued_evolution_source(job.model_copy(update={'source_message_id': None}),
                                   'secret') is None
    assert queued_evolution_source(job.model_copy(update={'request_id': 'auto-task'}),
                                   'secret') is None


async def test_missing_or_foreign_source_never_learns_or_applies(evolution):  # noqa: F811
    """没有真人来源的自动任务及不匹配会话不能获得个体人格。"""
    store, agent_id = evolution
    learner = EvolutionService(store, secret='offline-evolution-secret')
    req = AgentRequest(agent_code='helper', prompt='以后请简洁一点',
                       session_id='s', request_id='r')
    auth = ExecutionAuthority(entry='qq')
    assert await learner.suggestions(agent_id, req, auth) == ''
    token = current_conversation_source.set(ConversationSourceBinding(
        'another-session', {'source_kind': 'user', 'speaker_id': 'someone'}, START,
    ))
    try:
        assert await learner.suggestions(agent_id, req, auth) == ''
    finally:
        current_conversation_source.reset(token)
    assert (await store.profiles(agent_id))['items'] == []


async def test_admin_identity_survives_session_changes_and_isolated_from_qq(evolution):  # noqa: F811
    """认证管理对话跨浏览器会话积累，网页历史不能伪造 QQ 身份。"""
    store, agent_id = evolution
    learner = EvolutionService(store, secret='offline-evolution-secret')
    for session in ('admin_one', 'admin_two'):
        await learner.suggestions(agent_id, AgentRequest(
            agent_code='helper', prompt='以后请简洁一点', session_id=session, request_id='r',
        ), ExecutionAuthority(entry='admin'))
    profiles = (await store.profiles(agent_id))['items']
    assert len(profiles) == 1
    assert len((await store.history(agent_id, profiles[0]['id']))['items']) == 2
    other = EvolutionService(store, secret='new-session-secret')
    await other.suggestions(agent_id, AgentRequest(
        agent_code='helper', prompt='以后请简洁一点', session_id='admin_one', request_id='r',
    ), ExecutionAuthority(entry='admin'))
    assert len((await store.profiles(agent_id))['items']) == 2
