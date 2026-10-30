"""在所选业务库中原子保存反馈、有限状态与因果事件，不写运行配置。"""

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from agent_bot.agent.errors import AgentError
from agent_bot.ai.database import AiDatabase, query
from agent_bot.evolution.rules import TRAITS, extract_feedback


def _profile(row: dict) -> dict:
    """统一两种数据库的 JSON 返回值。"""
    return {**row, 'traits': json.loads(row['traits'])}


class EvolutionStore(AiDatabase):
    """演化范围行锁串行化观察、暂停与重置；角色关联可空且不拥有审计生命周期。"""

    def __init__(self, database, *, connect, clock: Callable[[], datetime] | None = None):
        """观察窗口以处理时钟为准，迟到消息不能排除已经收到的反向反馈。"""
        super().__init__(database, connect=connect)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def _agent(self, connection, agent_id: int, *, lock: bool = False) -> None:
        """显式关联必须指向真实 Agent；独立演化路径不调用此校验。"""
        row = query(connection, 'SELECT id FROM agent_bot_agent WHERE id=%s'
                    + (' FOR UPDATE' if lock else ''), (agent_id,), one=True)
        if row is None:
            raise AgentError('AGENT_CONFIG_NOT_FOUND', status_code=404)

    def _scope(self, connection, agent_id: int | None, *, write: bool = False) -> dict:
        """通用范围独立存在；角色范围仅在显式关联有效角色时按需建立。"""
        key = f'agent:{agent_id}' if agent_id is not None else 'global'
        if agent_id is not None:
            self._agent(connection, agent_id, lock=write)
        row = query(connection, 'SELECT * FROM agent_bot_evolution_scope WHERE scope_key=%s'
                    + (' FOR UPDATE' if write else ''), (key,), one=True)
        if row is None:
            row = {'scope_key': key, 'agent_id': agent_id, 'enabled': 1, 'revision': 0}
            if write:
                with connection.cursor() as cursor:
                    cursor.execute('INSERT INTO agent_bot_evolution_scope '
                                   '(scope_key,agent_id,enabled,revision) VALUES (%s,%s,1,0)',
                                   (key, agent_id))
        return row

    @staticmethod
    def _config(scope: dict) -> dict:
        """配置版本与画像版本独立，暂停不改写原角色或已有画像。"""
        return {'enabled': bool(scope['enabled']), 'revision': scope['revision']}

    async def configure(
        self, agent_id: int | None, *, expected_revision: int, enabled: bool,
    ) -> dict:
        """暂停与观察共用同一父锁，过期配置不覆盖新状态。"""
        def write(connection):
            """只有当前配置版本可以更新，首次保存也受版本约束。"""
            scope = self._scope(connection, agent_id, write=True)
            old = self._config(scope)
            if old['revision'] != expected_revision:
                raise AgentError('AGENT_CONFIG_CONFLICT', status_code=409)
            with connection.cursor() as cursor:
                cursor.execute('UPDATE agent_bot_evolution_scope SET enabled=%s,'
                               'revision=revision+1 WHERE scope_key=%s',
                               (enabled, scope['scope_key']))
            return {'enabled': enabled, 'revision': expected_revision + 1}
        return await self.run(write, write=True)

    async def profiles(self, agent_id: int | None, *, after: int = 0) -> dict:
        """画像按稳定主键分页，匿名主体不会作为其他用户的提示输入。"""
        def read(connection):
            """同时读取配置和当前页，不修改证据消费状态。"""
            scope = self._scope(connection, agent_id)
            rows = query(connection, 'SELECT p.*,s.agent_id FROM agent_bot_evolution_state p '
                         'JOIN agent_bot_evolution_scope s ON s.scope_key=p.scope_key '
                         'WHERE p.scope_key=%s AND p.id>%s ORDER BY p.id LIMIT 51',
                         (scope['scope_key'], after))
            return {'config': self._config(scope),
                    'items': [_profile(row) for row in rows[:50]],
                    'next_cursor': rows[49]['id'] if len(rows) > 50 else None}
        return await self.run(read)

    def _profile(self, connection, profile_id: int, *, lock: bool = False) -> dict:
        """画像可独立定位；锁定查询使用当前读，避免 MySQL RR 复用过时快照。"""
        row = query(connection, 'SELECT p.*,s.agent_id FROM agent_bot_evolution_state p '
                    'JOIN agent_bot_evolution_scope s ON s.scope_key=p.scope_key '
                    'WHERE p.id=%s' + (' FOR UPDATE' if lock else ''),
                    (profile_id,), one=True)
        if row is None:
            raise AgentError('AGENT_CONFIG_NOT_FOUND', status_code=404)
        return _profile(row)

    def _owned_profile(self, connection, agent_id: int | None, profile_id: int) -> dict:
        """旧角色接口保留归属校验，不能借别的角色读取或重置画像。"""
        scope = self._scope(connection, agent_id)
        profile = self._profile(connection, profile_id)
        if profile['scope_key'] != scope['scope_key']:
            raise AgentError('AGENT_CONFIG_NOT_FOUND', status_code=404)
        return profile

    async def history(self, agent_id: int | None, profile_id: int, *, before: int = 0) -> dict:
        """倒序分页返回原始反馈及因果引用，避免截断后无法追溯早期变化。"""
        def read(connection):
            """先验证画像归属，事件不能跨 Agent 查询。"""
            profile = self._owned_profile(connection, agent_id, profile_id)
            return self._history(connection, profile, before)
        return await self.run(read)

    def _history(self, connection, profile: dict, before: int) -> dict:
        """画像详情和全局记录详情共用有界历史分页。"""
        rows = query(connection, 'SELECT * FROM agent_bot_evolution_record '
                     'WHERE profile_id=%s' + (' AND id<%s' if before else '')
                     + ' ORDER BY id DESC LIMIT 51',
                     (profile['id'], before) if before else (profile['id'],))
        return {'profile': profile, 'items': [
            {**row, 'evidence_ids': json.loads(row['evidence_ids'])} for row in rows[:50]
        ], 'next_cursor': rows[49]['id'] if len(rows) > 50 else None}

    async def record_history(self, profile_id: int, *, before: int = 0) -> dict:
        """认证管理端可从跨范围列表直接查看依据，不要求先选择关联角色。"""
        return await self.run(lambda c: self._history(c, self._profile(c, profile_id), before))

    async def records(self, *, before: int = 0, kind: str | None = None,
                      agent_id: int | None = None, unbound: bool = False) -> dict:
        """跨范围记录倒序分页；类型和可选角色筛选不扩大普通对话的个人权限。"""
        conditions, args = [], []
        if before:
            conditions.append('r.id<%s')
            args.append(before)
        if kind is not None:
            conditions.append('r.kind=%s')
            args.append(kind)
        if agent_id is not None:
            conditions.append('s.agent_id=%s')
            args.append(agent_id)
        if unbound:
            conditions.append('s.agent_id IS NULL')
        where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''

        def read(connection):
            """单次联表获取记录及当前关联名称，不要求关联角色仍存在。"""
            rows = query(connection, 'SELECT r.*,p.subject_key,p.scope_key,s.agent_id,'
                         'a.name AS agent_name FROM agent_bot_evolution_record r '
                         'JOIN agent_bot_evolution_state p ON p.id=r.profile_id '
                         'JOIN agent_bot_evolution_scope s ON s.scope_key=p.scope_key '
                         'LEFT JOIN agent_bot_agent a ON a.id=s.agent_id' + where
                         + ' ORDER BY r.id DESC LIMIT 51', tuple(args))
            return {'items': [{**row, 'evidence_ids': json.loads(row['evidence_ids'])}
                              for row in rows[:50]],
                    'next_cursor': rows[49]['id'] if len(rows) > 50 else None}
        return await self.run(read)

    def _event(self, connection, profile_id: int, source_key: str, kind: str,
               dimension: str, direction: int, excerpt: str, stamp: datetime,
               *, before=None, after=None, evidence=()) -> int:
        """记录值均来自真实入口或固定状态机，不接受模型构造的来源引用。"""
        with connection.cursor() as cursor:
            cursor.execute('INSERT INTO agent_bot_evolution_record '
                           '(profile_id,source_key,kind,dimension,direction,excerpt,observed_at,'
                           'consumed,before_value,after_value,evidence_ids) '
                           'VALUES (%s,%s,%s,%s,%s,%s,%s,0,%s,%s,%s)',
                           (profile_id, source_key, kind, dimension, direction, excerpt, stamp,
                            before, after, json.dumps(evidence)))
            return cursor.lastrowid

    async def observe(self, agent_id: int | None, *, subject_key: str, source_key: str,
                      text: str | None, observed_at: datetime) -> dict | None:
        """读取已有习惯，并仅以真实当前反馈推动状态；重复来源不增加支持。"""
        if observed_at.utcoffset() is None:
            raise ValueError('evolution source time requires timezone')
        stamp = observed_at.astimezone(timezone.utc).replace(tzinfo=None)
        feedback = extract_feedback(text) if text else ()

        def write(connection):
            """锁定 Agent 后一次提交证据和状态，失败不会出现无依据的变化。"""
            scope = self._scope(connection, agent_id, write=True)
            if not scope['enabled']:
                return None
            row = query(connection, 'SELECT * FROM agent_bot_evolution_state '
                        'WHERE scope_key=%s AND subject_key=%s',
                        (scope['scope_key'], subject_key), one=True)
            if not feedback:
                return self._profile(connection, row['id']) if row else None
            if row is None:
                with connection.cursor() as cursor:
                    cursor.execute('INSERT INTO agent_bot_evolution_state '
                                   '(scope_key,subject_key,traits,revision) VALUES (%s,%s,%s,1)',
                                   (scope['scope_key'], subject_key,
                                    json.dumps(dict.fromkeys(TRAITS, 0))))
                    profile_id = cursor.lastrowid
                row = query(connection, 'SELECT * FROM agent_bot_evolution_state WHERE id=%s',
                            (profile_id,), one=True)
            profile = _profile(row)
            for dimension, direction in feedback:
                existing = query(connection, 'SELECT id FROM agent_bot_evolution_record '
                                 "WHERE profile_id=%s AND source_key=%s AND kind='feedback' "
                                 'AND dimension=%s', (row['id'], source_key, dimension), one=True)
                if existing:
                    continue
                self._event(connection, row['id'], source_key, 'feedback', dimension,
                            direction, text.strip(), stamp)
                now = self.clock().astimezone(timezone.utc).replace(tzinfo=None)
                self._advance(connection, profile, dimension, now)
            return self._owned_profile(connection, agent_id, row['id'])
        return await self.run(write, write=True)

    def _advance(self, connection, profile: dict, dimension: str, stamp: datetime) -> None:
        """九十天窗口内至少三日、七天跨度、净支持三次，消费后最多变化一档。"""
        rows = query(connection, 'SELECT id,direction,observed_at FROM agent_bot_evolution_record '
                     "WHERE profile_id=%s AND dimension=%s AND kind='feedback' AND consumed=0 "
                     'AND observed_at>=%s AND observed_at<=%s ORDER BY id',
                     (profile['id'], dimension, stamp - timedelta(days=90), stamp))
        net = sum(row['direction'] for row in rows)
        direction = 1 if net > 0 else -1
        dates = [datetime.fromisoformat(str(row['observed_at']))
                 for row in rows if row['direction'] == direction]
        if (abs(net) < 3 or len({date.date() for date in dates}) < 3
                or max(dates) - min(dates) < timedelta(days=7)):
            return
        before = profile['traits'][dimension]
        after = max(-3, min(3, before + direction))
        # 无论是否达到边界均消费成熟证据，避免未来反向变化时旧反馈重新强化。
        with connection.cursor() as cursor:
            cursor.execute('UPDATE agent_bot_evolution_record SET consumed=1 '
                           "WHERE profile_id=%s AND dimension=%s AND kind='feedback' "
                           'AND consumed=0 AND observed_at<=%s',
                           (profile['id'], dimension, stamp))
        if before == after:
            return
        profile['traits'][dimension] = after
        self._event(connection, profile['id'], uuid.uuid4().hex, 'change', dimension,
                    direction, '至少三个日期、跨度七天的明确反馈，反向反馈抵消后净支持达到三次。',
                    stamp, before=before, after=after, evidence=[row['id'] for row in rows])
        with connection.cursor() as cursor:
            cursor.execute('UPDATE agent_bot_evolution_state SET traits=%s,'
                           'revision=revision+1 WHERE id=%s',
                           (json.dumps(profile['traits']), profile['id']))

    async def reset(self, agent_id: int | None, profile_id: int, *, expected_revision: int) -> dict:
        """重置保留审计记录并消费所有旧反馈，旧请求重放也不能复活证据。"""
        def write(connection):
            """父锁覆盖版本检查、证据消费与画像清零。"""
            self._scope(connection, agent_id, write=True)
            old = self._owned_profile(connection, agent_id, profile_id)
            return self._reset(connection, old, expected_revision)
        return await self.run(write, write=True)

    async def reset_profile(self, profile_id: int, *, expected_revision: int) -> dict:
        """独立管理以画像定位并锁定所属范围，角色解除关联后仍可重置。"""
        def write(connection):
            """先定位范围，再加锁后重读版本，不能用锁前快照覆盖并发演化。"""
            old = self._profile(connection, profile_id)
            query(connection, 'SELECT scope_key FROM agent_bot_evolution_scope '
                  'WHERE scope_key=%s FOR UPDATE', (old['scope_key'],), one=True)
            return self._reset(connection, self._profile(connection, profile_id, lock=True),
                               expected_revision)
        return await self.run(write, write=True)

    def _reset(self, connection, old: dict, expected_revision: int) -> dict:
        """调用方已锁定范围，清零与消费旧证据在同一事务提交。"""
        if old['revision'] != expected_revision:
            raise AgentError('AGENT_CONFIG_CONFLICT', status_code=409)
        profile_id = old['id']
        with connection.cursor() as cursor:
            cursor.execute('UPDATE agent_bot_evolution_state SET traits=%s,'
                           'revision=revision+1 WHERE id=%s',
                           (json.dumps(dict.fromkeys(TRAITS, 0)), profile_id))
            cursor.execute('UPDATE agent_bot_evolution_record SET consumed=1 '
                           'WHERE profile_id=%s', (profile_id,))
        self._event(connection, profile_id, uuid.uuid4().hex, 'reset', '', 0,
                    '管理员重置表达习惯；此前反馈不再参与演化。',
                    self.clock().astimezone(timezone.utc).replace(tzinfo=None))
        return self._profile(connection, profile_id)
