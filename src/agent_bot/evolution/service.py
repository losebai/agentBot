"""将服务端绑定的真人来源映射到匿名演化画像，不读取模型历史作为证据。"""

import hashlib
import hmac
import json
from datetime import datetime, timezone

from agent_bot.agent.contracts import AgentRequest, ExecutionAuthority
from agent_bot.evolution.rules import TRAITS, evolution_prompt
from agent_bot.evolution.store import EvolutionStore
from agent_bot.state import current_conversation_source


class EvolutionService:
    """会话密钥隔离主体命名空间；跨会话保持身份，不向提示词泄露原始用户标识。"""

    def __init__(self, store: EvolutionStore, *, secret: str):
        """存储与会话密钥由当前核心运行时拥有，热刷新不缓存旧配置。"""
        self.store = store
        self._secret = secret.encode()

    def _key(self, *parts: str) -> str:
        """不同用途独立加域，防止消息引用与主体摘要互相复用。"""
        return hmac.new(self._secret, json.dumps(('evolution', *parts)).encode(),
                        hashlib.sha256).hexdigest()

    async def suggestions(
        self, agent_id: int | None, request: AgentRequest, authority: ExecutionAuthority,
    ) -> str:
        """只有明确真人来源才能学习或应用个人习惯，自动任务保持原角色。"""
        if authority.entry == 'admin':
            subject = self._key('admin')
            source = self._key('admin-source', request.session_id, request.request_id)
            text, stamp = request.prompt, datetime.now(timezone.utc)
        else:
            binding = current_conversation_source.get()
            if binding is None or binding.session_id != request.session_id:
                return ''
            facts = binding.source
            if (facts.get('source_kind') != 'user' or facts.get('platform') != 'qq'
                    or not facts.get('speaker_id') or not facts.get('source_event_id')
                    or not facts.get('bot_id') or facts.get('source_text') is None):
                return ''
            subject = self._key('qq', facts['bot_id'], facts['speaker_id'])
            source = self._key('qq-source', facts['bot_id'], facts['source_event_id'])
            text, stamp = facts['source_text'], binding.created_at
        general = await self.store.observe(None, subject_key=subject, source_key=source,
                                           text=text, observed_at=stamp)
        traits = dict(general['traits']) if general else dict.fromkeys(TRAITS, 0)
        if agent_id is not None:
            # 可选专属范围与通用范围分开保存，同维度的非零专属习惯优先。
            specific = await self.store.observe(agent_id, subject_key=subject, source_key=source,
                                                text=text, observed_at=stamp)
            if specific:
                traits.update({key: value for key, value in specific['traits'].items() if value})
        return evolution_prompt(traits)
