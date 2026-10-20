"""在异步收发链路中复用统一会话仓库，只恢复已有确认回执，不重试平台副作用。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from functools import partial
from typing import TYPE_CHECKING

from agent_bot.database_pool import finish_database_write
from agent_bot.services.tool_operations import DeliveryEffect, ToolOperationStore
from agent_bot.state import ConversationContext, MemoryCompression

if TYPE_CHECKING:
    from agent_bot.session_context import ContextDelivery, SessionContextStore


class SessionDeliveryStore:
    """将当前运行时的同步会话仓库适配为异步接口，不读取旧出站正文表。"""

    def __init__(
        self,
        store_provider: Callable[[], SessionContextStore],
        *,
        receipts_provider: Callable[[], ToolOperationStore] | None = None,
        live_receipts_provider: Callable[[], dict[str, DeliveryEffect]] | None = None,
        memory_store_provider: Callable[[], object | None] | None = None,
    ) -> None:
        """每次操作固定一个运行时仓库，已开始的写事务即使取消也等待完成。"""
        self._store_provider = store_provider
        self._receipts_provider = receipts_provider
        self._live_receipts_provider = live_receipts_provider
        self._memory_store_provider = memory_store_provider

    async def context(
        self,
        session_id: str,
        *,
        exclude_request_id: str | None = None,
    ) -> ConversationContext:
        """先恢复平台已确认的占位，再从同一消息流读取排除当前用户输入的快照。"""
        store = self._store_provider()
        context = await finish_database_write(
            partial(
                store.context,
                session_id,
                exclude_request_id=exclude_request_id,
            )
        )
        if context.generation_id is None:
            return context
        await self._reconcile(store, session_id, context.generation_id)
        return await finish_database_write(
            partial(
                store.context,
                session_id,
                exclude_request_id=exclude_request_id,
            )
        )

    async def record_received(
        self,
        session_id: str,
        content: str,
        *,
        request_id: str,
        created_at: datetime | None = None,
        generation_id: str | None = None,
        source: dict[str, str] | None = None,
    ) -> MemoryCompression | None:
        """原样传递用户事件时间与代际，由仓库完成原子追加和请求去重。"""
        store = self._store_provider()
        source_options = {"source": source} if source is not None else {}
        return await finish_database_write(
            lambda: store.record_received(
                session_id,
                content,
                request_id=request_id,
                created_at=created_at,
                generation_id=generation_id,
                **source_options,
            )
        )

    async def finish_received(
        self,
        session_id: str,
        request_id: str,
    ) -> MemoryCompression | None:
        """结束用户请求的在途保护；失败输入可压缩，但不伪造助手回复。"""
        store = self._store_provider()
        return await finish_database_write(partial(store.finish_request, session_id, request_id))

    async def claim_delivery(
        self,
        session_id: str,
        generation_id: str,
        delivery_id: str,
        request_id: str | None,
        message_id: str | None,
        content: str,
        material_ids: tuple[str, ...],
    ) -> ContextDelivery | None:
        """发送前持久占位；未知结果由仓库拒绝，禁止自动重发。"""
        store = self._store_provider()
        return await finish_database_write(
            lambda: store.claim_delivery(
                session_id=session_id,
                generation_id=generation_id,
                delivery_id=delivery_id,
                request_id=request_id,
                message_id=message_id,
                content=content,
                material_ids=material_ids,
            )
        )

    async def finish_delivery(
        self,
        delivery_id: str,
        message_id: str | None,
        *,
        session_id: str,
    ) -> ContextDelivery:
        """平台成功且回执已保存后，按明确 session 完成原投递占位。"""
        store = self._store_provider()
        delivery = await finish_database_write(
            lambda: store.finish_delivery(session_id, delivery_id, message_id),
        )
        if self._memory_store_provider is not None:
            memory = self._memory_store_provider()
            if memory is not None:
                # 平台确认与上下文成功后才计采用；重试依靠请求/事实版本回执去重。
                await finish_database_write(
                    lambda: memory.record_adoption(
                        session_id, delivery.request_id, delivery.content
                    ),
                )
        return delivery

    async def record(
        self,
        session_id: str,
        generation_id: str,
        delivery_id: str,
        request_id: str | None,
        message_id: str | None,
        content: str,
        material_ids: tuple[str, ...],
    ) -> bool:
        """导入明确已发送的消息；代际和幂等校验均交给统一仓库。"""
        store = self._store_provider()
        return await finish_database_write(
            lambda: store.record(
                session_id=session_id,
                generation_id=generation_id,
                delivery_id=delivery_id,
                request_id=request_id,
                message_id=message_id,
                content=content,
                material_ids=material_ids,
            )
        )

    async def list_messages(
        self,
        session_id: str,
        generation_id: str,
        limit: int = 100,
    ) -> tuple[ContextDelivery, ...]:
        """只列出指定代际的成功消息，补记已确认回执但不触发任何平台调用。"""
        store = self._store_provider()
        await self._reconcile(store, session_id, generation_id)
        return await finish_database_write(
            partial(
                store.list_messages,
                session_id,
                generation_id,
                limit,
            )
        )

    async def _reconcile(
        self,
        store: SessionContextStore,
        session_id: str,
        generation_id: str,
    ) -> None:
        """只核对当前会话的 pending 身份；上下文确认失败时保留内存回执供重试。"""
        if self._receipts_provider is None and self._live_receipts_provider is None:
            return
        identities = await finish_database_write(
            partial(
                store.pending_deliveries,
                session_id,
                generation_id,
            )
        )
        receipts = self._receipts_provider() if self._receipts_provider else None
        live = self._live_receipts_provider() if self._live_receipts_provider else {}
        for delivery_id in identities:
            effect = live.get(delivery_id)
            if effect is None and receipts is not None:
                effect = await receipts.completed(delivery_id, "outgoing_message")
            if isinstance(effect, DeliveryEffect):
                await finish_database_write(
                    partial(
                        store.finish_delivery,
                        session_id,
                        delivery_id,
                        effect.message_id,
                    )
                )
                live.pop(delivery_id, None)
