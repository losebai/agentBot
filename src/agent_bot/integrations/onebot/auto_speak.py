"""由显式选择的 Laya 或独立 LLM 决策门控的群聊自动发言。"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from agent_bot.group_context import RecentGroupContextStoreProtocol
from agent_bot.models import ChatToolName
from agent_bot.services.auto_speak_decision import (
    AutoSpeakContextBuilder,
    AutoSpeakDecisionClient,
    AutoSpeakTriggerMode,
    build_llm_decision_state,
)
from agent_bot.services.chat.routing import ChatRouteSelector
from agent_bot.services.conversation_queue import ConversationJob
from agent_bot.state import ConversationContext, build_session_scope, external_session_id


class AutoSpeakBot(Protocol):
    """自动发言流程使用的最小 OneBot 接口。"""

    self_id: str


BotProvider = Callable[[str], AutoSpeakBot | None]
Sleep = Callable[[float], Awaitable[None]]
ErrorReporter = Callable[[str], None]
JobSubmitter = Callable[[ConversationJob], Awaitable[str]]
AvailableToolsProvider = Callable[[], Sequence[ChatToolName]]
ConversationContextProvider = Callable[[str], Awaitable[ConversationContext]]

_DECISION_LOCK = asyncio.Lock()
_AUTO_SPEAK_REQUEST = "请结合当前自动发言会话历史和群聊上下文，生成本次主动发言。"


@dataclass(frozen=True, slots=True)
class AutoSpeakCycleResult:
    """一次自动发言周期的脱敏统计。"""

    bot_available: bool
    sent: int
    failed: int


class AutoSpeakService:
    """顺序检查配置群，并仅在所选决策服务允许后生成消息。"""

    def __init__(
        self,
        *,
        job_submitter: JobSubmitter,
        available_tools_provider: AvailableToolsProvider,
        conversation_context_provider: ConversationContextProvider,
        decision_client: AutoSpeakDecisionClient,
        context_builder: AutoSpeakContextBuilder,
        recent_context_store: RecentGroupContextStoreProtocol,
        bot_provider: BotProvider,
        bot_id: str,
        group_ids: set[str] | frozenset[str],
        trigger_mode: AutoSpeakTriggerMode,
        prompt: str,
        system_prompt: str,
        laya_prompt: str,
        session_secret: str,
        initial_delay_seconds: float,
        interval_seconds: float,
        max_reply_chars: int,
        sleep: Sleep = asyncio.sleep,
        on_error: ErrorReporter | None = None,
        system_prompt_provider: Callable[[], Awaitable[str | None]] | None = None,
        group_shared_session: bool = False,
        decision_backend: Literal["laya", "llm"] = "laya",
    ) -> None:
        """保存群目标和会话边界；LLM 只接收群聊快照，隔离会话记忆和人设。"""

        if (
            not bot_id
            or not group_ids
            or not prompt.strip()
            or not laya_prompt.strip()
            or not session_secret
        ):
            raise ValueError("auto-speak identity, groups, prompt, and session secret are required")
        if initial_delay_seconds < 0 or interval_seconds <= 0 or max_reply_chars <= 0:
            raise ValueError("auto-speak timing and reply limits are invalid")

        self._job_submitter = job_submitter
        self._available_tools_provider = available_tools_provider
        self._conversation_context_provider = conversation_context_provider
        self._decision_client = decision_client
        self._context_builder = context_builder
        self._recent_context_store = recent_context_store
        self._bot_provider = bot_provider
        self._bot_id = bot_id
        self._group_ids = tuple(sorted(group_ids))
        self._group_id_set = frozenset(group_ids)
        self._trigger_mode = trigger_mode
        self._prompt = prompt.strip()
        self._system_prompt = system_prompt.strip()
        self._system_prompt_provider = system_prompt_provider
        self._laya_prompt = laya_prompt.strip()
        self._session_secret = session_secret
        self._group_shared_session = group_shared_session
        self._decision_backend = decision_backend
        self._initial_delay_seconds = initial_delay_seconds
        self._interval_seconds = interval_seconds
        self._max_reply_chars = max_reply_chars
        self._sleep = sleep
        self._on_error = on_error

    @property
    def runs_on_interval(self) -> bool:
        """Return whether this immutable runtime owns the interval trigger."""

        return self._trigger_mode == "interval"

    def accepts_message(self, bot_id: str, group_id: str) -> bool:
        """Return whether a group message belongs to this message-trigger runtime."""

        return (
            self._trigger_mode == "message"
            and bot_id == self._bot_id
            and group_id in self._group_id_set
        )

    async def run(self) -> None:
        """仅在 interval 模式持续运行，每轮完成后等待完整间隔。"""

        if not self.runs_on_interval:
            return
        await self._sleep(self._initial_delay_seconds)
        while True:
            try:
                await self.run_once()
            except Exception as exc:
                self._report_error(exc)
            await self._sleep(self._interval_seconds)

    async def run_once(self) -> AutoSpeakCycleResult:
        """重新获取 Bot，并顺序判断和处理一次全部群目标。"""

        try:
            bot = self._bot_provider(self._bot_id)
        except Exception as exc:
            self._report_error(exc)
            return AutoSpeakCycleResult(bot_available=False, sent=0, failed=0)
        if bot is None:
            return AutoSpeakCycleResult(bot_available=False, sent=0, failed=0)

        sent = 0
        failed = 0
        for group_id in self._group_ids:
            try:
                context = await asyncio.to_thread(
                    self._recent_context_store.context,
                    bot_id=self._bot_id,
                    group_id=group_id,
                )
                if await self._evaluate_group(bot, group_id, context):
                    sent += 1
            except Exception as exc:
                failed += 1
                self._report_error(exc)
        return AutoSpeakCycleResult(bot_available=True, sent=sent, failed=failed)

    async def handle_group_message(
        self,
        bot: AutoSpeakBot,
        group_id: str,
        recent_group_context: str | None,
    ) -> bool:
        """处理一条已筛选群消息；任何当前目标失败都保持聊天侧静默。"""

        if not self.accepts_message(str(bot.self_id), group_id):
            return False
        try:
            return await self._evaluate_group(bot, group_id, recent_group_context)
        except Exception as exc:
            self._report_error(exc)
            return False

    async def _evaluate_group(
        self,
        bot: AutoSpeakBot,
        group_id: str,
        recent_group_context: str | None,
    ) -> bool:
        """按配置绑定发送会话；LLM 决策独立于该会话的记忆、历史和 Agent。"""

        # 共享群复用普通事件的规范身份；未共享时不能借主动发言扩大用户素材权限。
        scope = (
            build_session_scope(
                bot_id=self._bot_id, user_id=self._bot_id, group_id=group_id, group_shared=True,
            )
            if self._group_shared_session
            else f"bot:{self._bot_id}:auto:group:{group_id}"
        )
        session_id = external_session_id(scope, self._session_secret)
        if self._decision_backend == "llm":
            # 消息触发快照已包含当前消息，无需再次拼接，也不读取会话记忆或人设。
            state = build_llm_decision_state(recent_group_context)
        else:
            conversation_context = await self._conversation_context_provider(session_id)
            current_prompt = (
                await self._system_prompt_provider() if self._system_prompt_provider else None
            )
            state = self._context_builder.build(
                trigger_mode=self._trigger_mode,
                system_prompt=self._system_prompt if current_prompt is None else current_prompt,
                auto_speak_prompt=self._prompt,
                laya_prompt=self._laya_prompt,
                conversation_context=conversation_context,
                recent_group_context=recent_group_context,
            )
        async with _DECISION_LOCK:
            result = await self._decision_client.decide_auto_speak(
                state=state,
                session_id=session_id,
                request_id=f"auto-decision-{uuid.uuid4().hex}",
            )
        if not result.should_speak:
            return False
        await self._enqueue_speak(bot, group_id, session_id, recent_group_context)
        return True

    async def _enqueue_speak(
        self,
        bot: AutoSpeakBot,
        group_id: str,
        session_id: str,
        recent_group_context: str | None,
    ) -> None:
        """把获准发言固化为普通群聊任务，由统一队列生成、调用工具并投递。"""

        tools = ChatRouteSelector().select(
            self._prompt,
            available_tools=self._available_tools_provider(),
        ).chat_tools
        job_id = uuid.uuid4().hex
        await self._job_submitter(
            ConversationJob(
                jobId=job_id,
                requestId=f"auto-{job_id}",
                createdAt=time.time(),
                sessionId=session_id,
                botId=str(bot.self_id),
                scene="group",
                groupId=group_id,
                userId=str(bot.self_id),
                prompt=_AUTO_SPEAK_REQUEST,
                chatTools=tools,
                recentGroupContext=recent_group_context,
                autoSpeakMaxReplyChars=self._max_reply_chars,
                systemPromptAppend=self._prompt,
            )
        )

    def _report_error(self, exc: Exception) -> None:
        """仅上报异常类型，失败时绝不向群发送状态文案。"""

        if self._on_error is None:
            return
        try:
            self._on_error(type(exc).__name__)
        except Exception:
            return
