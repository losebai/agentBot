"""演化查询和控制必须认证，修改只能使用当前配置或画像版本。"""

from datetime import datetime, timezone
from pathlib import Path

from agent_bot.evolution.service import EvolutionService
from agent_bot.evolution.store import EvolutionStore

from .test_agent_routes import agent_api  # noqa: F401
from .test_ai_routes import HEADERS

EVOLUTION_SQL = (Path('sql/agent-evolution.sqlite.sql').read_text('utf-8')
                 + Path('sql/evolution-independent.sqlite.sql').read_text('utf-8'))


async def test_evolution_admin_control_requires_authentication_and_revision(
    agent_api, agent_database, agent_params,  # noqa: F811
):
    """不能伪造证据；旧版本启停和跨 Agent 画像访问均拒绝。"""
    api, _, runtime = agent_api
    agent_database.connection.executescript(
        EVOLUTION_SQL,
    )
    store = EvolutionStore(agent_params, connect=agent_database.connect)
    runtime.agent_service.evolution = EvolutionService(store, secret='offline')
    path = '/api/v1/agents/1/evolution'
    assert (await api.get(path)).status_code == 401
    response = await api.get(path, headers=HEADERS)
    assert response.status_code == 200
    assert response.json()['config'] == {'enabled': True, 'revision': 0}
    profile = await store.observe(1, subject_key='a' * 64, source_key='b' * 64,
                                  text='以后请简洁一点', observed_at=datetime.now(timezone.utc))
    history = f'{path}/profiles/{profile["id"]}'
    assert (await api.get(history, headers=HEADERS)).json()['items'][0]['excerpt'] == (
        '以后请简洁一点'
    )
    assert (await api.get('/api/v1/agents/999/evolution/profiles/1',
                          headers=HEADERS)).status_code == 404
    body = {'expected_revision': 0, 'enabled': False}
    assert (await api.patch(path, headers=HEADERS, json=body)).status_code == 200
    assert (await api.patch(path, headers=HEADERS, json=body)).status_code == 409
    assert (await api.post(f'{history}/reset', headers=HEADERS,
                           json={'expected_revision': 1})).status_code == 200
    assert (await api.post(f'{history}/reset', headers=HEADERS,
                           json={'expected_revision': 1})).status_code == 409
    assert (await api.patch(path, headers=HEADERS,
                            json={**body, 'evidence': '伪造'})).status_code == 422


async def test_independent_routes_allow_unbound_state_and_record_listing(
    agent_api, agent_database, agent_params,  # noqa: F811
):
    """独立入口无需角色，可直接查记录、查看依据及按版本重置。"""
    api, _, runtime = agent_api
    agent_database.connection.executescript(EVOLUTION_SQL)
    store = EvolutionStore(agent_params, connect=agent_database.connect)
    runtime.agent_service.evolution = EvolutionService(store, secret='offline')
    path = '/api/v1/evolution'
    assert (await api.get(path)).status_code == 401
    assert (await api.get(path + '/records')).status_code == 401
    response = await api.get(path, headers=HEADERS)
    assert response.status_code == 200 and response.json()['items'] == []
    profile = await store.observe(None, subject_key='a' * 64, source_key='b' * 64,
                                  text='以后请简洁一点', observed_at=datetime.now(timezone.utc))
    rows = (await api.get(path + '/records', headers=HEADERS)).json()['items']
    assert rows[0]['agent_id'] is None and rows[0]['profile_id'] == profile['id']
    assert (await api.get(path + '/records?kind=invalid', headers=HEADERS)).status_code == 422
    history = f'{path}/profiles/{profile["id"]}'
    assert (await api.get(history, headers=HEADERS)).json()['items'][0]['excerpt'] == (
        '以后请简洁一点'
    )
    assert (await api.post(history + '/reset', headers=HEADERS,
                           json={'expected_revision': 1})).status_code == 200
    assert (await api.post(history + '/reset', headers=HEADERS,
                           json={'expected_revision': 1})).status_code == 409
    config = {'expected_revision': 0, 'enabled': False}
    assert (await api.patch(path, headers=HEADERS, json=config)).status_code == 200
    assert (await api.patch(path, headers=HEADERS, json=config)).status_code == 409
