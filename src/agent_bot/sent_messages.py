"""持久登记实际出站消息，并按当前会话代际合并模型可见历史。"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agent_bot.ai.database import AiDatabase, query
from agent_bot.database import database_connector
from agent_bot.models import ChatMessage
from agent_bot.services.tool_operations import DeliveryEffect, ToolOperationStore
from agent_bot.settings import DatabaseParams
from agent_bot.state import ConversationContext, validate_request_id

_COLUMNS = "delivery_id,request_id,message_id,content,material_ids,created_at"
# GeneratedReply 允许一百万字符，另为网关追加的素材标识和名称预留有界空间。
_MAX_GENERATED_TEXT_CHARS = 1_000_000
_MAX_MATERIAL_REFERENCE_CHARS = 32_000
# UTF-8 每字符最多四字节，完整日志最多约 4.128 MB，仍低于 MEDIUMTEXT 的 16 MiB。
_MAX_CONTENT_CHARS = _MAX_GENERATED_TEXT_CHARS + _MAX_MATERIAL_REFERENCE_CHARS


class SentMessageUncertainError(RuntimeError):
    """已有发送占位但平台结果未知，禁止自动重发。"""


@dataclass(frozen=True, slots=True)
class SentMessage:
    """一条已成功发送消息的不可变快照，素材正文只保留已渲染引用。"""

    delivery_id: str
    request_id: str | None
    content: str
    material_ids: tuple[str, ...]
    created_at: datetime
    message_id: str | None = None


class SentMessageStore(AiDatabase):
    """以独立发送占位阻止重发，日志保留原代际且不级联删除。"""

    def __init__(
        self, params: DatabaseParams, *, connect: Callable[..., Any] | None = None,
        clock: Callable[[], datetime] | None = None,
        receipts_provider: Callable[[], ToolOperationStore] | None = None,
        live_receipts_provider: Callable[[], dict[str, DeliveryEffect]] | None = None,
    ) -> None:
        """绑定显式数据库与时钟，已有部署不会自动初始化或升级。"""
        super().__init__(params, connect=connect or database_connector(params))
        self._clock = clock or (lambda: datetime.now(UTC))
        self._receipts_provider = receipts_provider
        self._live_receipts_provider = live_receipts_provider

    def _now(self) -> datetime:
        """统一以无时区 UTC 写入两种数据库的时间列。"""
        value = self._clock()
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value

    async def claim_delivery(
        self, session_id: str, generation_id: str, delivery_id: str,
        request_id: str | None, message_id: str | None, content: str,
        material_ids: tuple[str, ...],
    ) -> SentMessage | None:
        """网络发送前提交占位；已完成可复用，未知结果必须显式处理。"""
        values = _validated_values(session_id, generation_id, delivery_id, request_id,
                                   message_id, content, material_ids)
        now = self._now()

        def claim(connection: Any) -> SentMessage | None:
            """唯一键仲裁跨进程发送者，不持有数据库事务等待平台网络。"""
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT IGNORE INTO chat_sent_message "
                    "(session_id,generation_id,delivery_id,request_id,message_id,content,"
                    "material_ids,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (*values, "pending", now),
                )
                inserted = cursor.rowcount == 1
            row = query(connection, "SELECT * FROM chat_sent_message "
                        "WHERE delivery_id=%s FOR UPDATE", (delivery_id,), one=True)
            _check_identity(row, values)
            if inserted:
                return None
            if row["status"] != "sent":
                raise SentMessageUncertainError("sent message delivery outcome is uncertain")
            return _snapshot(row)

        return await self.run(claim, write=True)

    async def finish_delivery(
        self, delivery_id: str, message_id: str | None, *, session_id: str | None = None,
    ) -> SentMessage:
        """旧出站表按全局投递 ID 确认；仅为 legacy 网关兼容接收并忽略 session_id。"""
        _validate_delivery_id(delivery_id)
        _validate_message_id(message_id)
        now = self._now()

        def finish(connection: Any) -> SentMessage:
            """完成时间只写一次，重复完成不会改变历史排序或平台标识。"""
            row = query(connection, "SELECT * FROM chat_sent_message "
                        "WHERE delivery_id=%s FOR UPDATE", (delivery_id,), one=True)
            if row is None:
                raise ValueError("sent message delivery was not claimed")
            if row["status"] == "sent":
                if row["message_id"] != message_id:
                    raise ValueError("sent message delivery receipt conflicts")
                return _snapshot(row)
            query(connection, "UPDATE chat_sent_message SET status=%s,message_id=%s,"
                  "created_at=%s WHERE delivery_id=%s", ("sent", message_id, now, delivery_id))
            return _snapshot(row | {"status": "sent", "message_id": message_id,
                                    "created_at": now})

        return await self.run(finish, write=True)

    async def record(
        self, session_id: str, generation_id: str, delivery_id: str,
        request_id: str | None, message_id: str | None, content: str,
        material_ids: tuple[str, ...],
    ) -> bool:
        """导入已确认发送，原子拒绝过期代际并按投递标识去重。"""
        values = _validated_values(session_id, generation_id, delivery_id, request_id,
                                   message_id, content, material_ids)
        now = self._now()

        def save(connection: Any) -> bool:
            """锁住当前会话，避免清空历史与写入成功日志之间交错。"""
            current = query(connection, "SELECT generation_id FROM chat_memory_session "
                            "WHERE session_id=%s AND expires_at>%s FOR UPDATE",
                            (session_id, now), one=True)
            if current is None or current["generation_id"] != generation_id:
                return False
            with connection.cursor() as cursor:
                cursor.execute(
                    "INSERT IGNORE INTO chat_sent_message "
                    "(session_id,generation_id,delivery_id,request_id,message_id,content,"
                    "material_ids,status,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (*values, "sent", now),
                )
                inserted = cursor.rowcount == 1
            if not inserted:
                row = query(connection, "SELECT * FROM chat_sent_message "
                            "WHERE delivery_id=%s FOR UPDATE", (delivery_id,), one=True)
                _check_identity(row, values)
                if row["status"] != "sent":
                    raise SentMessageUncertainError("sent message delivery outcome is uncertain")
            return inserted

        return await self.run(save, write=True)

    async def list_messages(
        self, session_id: str, generation_id: str, limit: int = 100,
    ) -> tuple[SentMessage, ...]:
        """仅返回当前有效代际的最新成功消息，以完成时间和主键稳定排序。"""
        _validate_scope(session_id, generation_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("sent message limit must be between 1 and 100")
        if self._receipts_provider is not None or self._live_receipts_provider is not None:
            await self._reconcile(session_id, generation_id)
        columns = ",".join("m." + column for column in _COLUMNS.split(","))
        rows = await self.run(lambda connection: query(
            connection, f"SELECT {columns} FROM chat_sent_message m "
            "JOIN chat_memory_session s ON s.session_id=m.session_id "
            "AND s.generation_id=m.generation_id WHERE m.session_id=%s "
            "AND m.generation_id=%s AND m.status=%s AND s.expires_at>%s "
            "ORDER BY m.created_at DESC,m.id DESC LIMIT %s",
            (session_id, generation_id, "sent", self._now(), limit),
        ))
        return tuple(_snapshot(row) for row in reversed(rows))

    async def _reconcile(self, session_id: str, generation_id: str) -> None:
        """读取上下文时核对持久回执，独立通知无需重新触发发送即可补记成功。"""
        rows = await self.run(lambda connection: query(
            connection, "SELECT delivery_id FROM chat_sent_message "
            "WHERE session_id=%s AND generation_id=%s AND status=%s "
            "ORDER BY id DESC LIMIT 100", (session_id, generation_id, "pending"),
        ))
        receipts = self._receipts_provider() if self._receipts_provider else None
        live = self._live_receipts_provider() if self._live_receipts_provider else {}
        # 查询取最新有界批次；按原占位先后补记，避免恢复时倒置已知顺序。
        for row in reversed(rows):
            identity = row["delivery_id"]
            effect = live.get(identity)
            if effect is None and receipts is not None:
                effect = await receipts.completed(identity, "outgoing_message")
            if isinstance(effect, DeliveryEffect):
                await self.finish_delivery(identity, effect.message_id)
                live.pop(identity, None)


def _snapshot(row: dict[str, Any]) -> SentMessage:
    """数据库 JSON 只解码为素材标识，不读取文件或注入模型图片。"""
    return SentMessage(
        delivery_id=row["delivery_id"], request_id=row["request_id"], content=row["content"],
        material_ids=tuple(json.loads(row["material_ids"])), created_at=row["created_at"],
        message_id=row["message_id"],
    )


def _validate_scope(session_id: str, generation_id: str) -> None:
    """沿用会话标识上限，代际只接受系统生成的 UUID 十六进制值。"""
    if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
        raise ValueError("invalid sent message session")
    if not isinstance(generation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", generation_id):
        raise ValueError("invalid sent message generation")


def _validate_delivery_id(delivery_id: str) -> None:
    """投递标识必须是网关生成的稳定 SHA256，避免跨目标复用任意短键。"""
    if not isinstance(delivery_id, str) or not re.fullmatch(r"[a-f0-9]{64}", delivery_id):
        raise ValueError("invalid sent message delivery id")


def _validate_message_id(message_id: str | None) -> None:
    """平台没有返回标识时允许空值，已返回的标识必须有界。"""
    if message_id is not None and (
        not isinstance(message_id, str) or not message_id or len(message_id) > 128
    ):
        raise ValueError("invalid sent message platform id")


def _validated_values(
    session_id: str, generation_id: str, delivery_id: str, request_id: str | None,
    message_id: str | None, content: str, material_ids: tuple[str, ...],
) -> tuple[Any, ...]:
    """限制日志大小并复用现有请求校验，保留已渲染正文而不推测路径。"""
    _validate_scope(session_id, generation_id)
    _validate_delivery_id(delivery_id)
    _validate_message_id(message_id)
    validate_request_id(request_id)
    if not isinstance(content, str) or not content or len(content) > _MAX_CONTENT_CHARS:
        raise ValueError("invalid sent message content")
    if not isinstance(material_ids, tuple) or len(material_ids) > 100 or any(
        not isinstance(value, str) or not value or len(value) > 128 for value in material_ids
    ):
        raise ValueError("invalid sent message materials")
    return (session_id, generation_id, delivery_id, request_id, message_id, content,
            json.dumps(material_ids, ensure_ascii=False))


def _check_identity(row: dict[str, Any], values: tuple[Any, ...]) -> None:
    """幂等键只对应原目标和原内容，不能借旧回执确认另一条消息。"""
    columns = ("session_id", "generation_id", "delivery_id", "request_id", "message_id",
               "content", "material_ids")
    if any(row[column] != value for column, value in zip(columns, values, strict=True)
           if column != "message_id"):
        raise ValueError("sent message delivery identity conflicts")


def merge_sent_messages(
    context: ConversationContext, rows: Sequence[SentMessage], *,
    max_chars: int = 80_000, max_message_chars: int = 32_000,
) -> ConversationContext:
    """以成功发送替换摘要并限制模型视图，原始日志和独立助手事件保持不变。"""
    if not rows:
        return context
    if (
        isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0
        or isinstance(max_message_chars, bool) or not isinstance(max_message_chars, int)
        or max_message_chars <= 0
    ):
        raise ValueError("invalid sent message model context limits")
    request_ids = {row.request_id for row in rows if row.request_id is not None}
    users: dict[str, tuple[int, ChatMessage]] = {}
    legacy: list[tuple[int, ChatMessage]] = []
    for index, message in enumerate(context.messages):
        request_id = message.history_request_id
        if request_id in request_ids and message.role in {"user", "assistant"}:
            if message.role == "user":
                users.setdefault(request_id, (index, message))
        else:
            legacy.append((index, message))
    messages: list[ChatMessage] = []
    user_sizes = {request: len(message.content) for request, (_, message) in users.items()}
    legacy_index = 0
    # 没有真实发送时间的旧轮次保留在首个新请求之前；只有通知时旧历史整体在前。
    first_user_index = min((index for index, _ in users.values()), default=len(context.messages))
    while legacy_index < len(legacy) and legacy[legacy_index][0] < first_user_index:
        messages.append(legacy[legacy_index][1])
        legacy_index += 1
    # 调用方按日志完成时间读取；相同时间保留数据库主键顺序。
    for row in sorted(rows, key=lambda item: item.created_at):
        matched = users.pop(row.request_id, None)
        if matched is not None:
            index, user = matched
            while legacy_index < len(legacy) and legacy[legacy_index][0] < index:
                messages.append(legacy[legacy_index][1])
                legacy_index += 1
            messages.append(user)
        # 单个大文件预览不能占满预算后再挤掉关联的用户问题。
        row_limit = min(max_message_chars, max_chars - user_sizes.get(row.request_id, 0))
        if row_limit <= 0:
            continue
        content = _model_content(row.content, row.material_ids, row_limit)
        if not content:
            continue
        messages.append(ChatMessage(
            role="assistant",
            content=content,
            history_request_id=row.request_id,
        ))
    messages.extend(message for _, message in legacy[legacy_index:])
    # 旧轮次也可能保存完整文件，只限制此次模型副本，不回写历史或日志。
    bounded = tuple(message.model_copy(update={
        "content": _model_content(message.content, (), max_message_chars),
    }) for message in messages if message.content)
    total_chars = sum(len(message.content) for message in bounded)
    start = 0
    while total_chars > max_chars:
        total_chars -= len(bounded[start].content)
        start += 1
    return ConversationContext(memory=context.memory, messages=bounded[start:],
                               generation_id=context.generation_id)


def _model_content(content: str, material_ids: tuple[str, ...], max_chars: int) -> str:
    """裁切模型预览时预留原件读取提示与全部素材标识，不裁切持久正文。"""
    if len(content) <= max_chars:
        return content
    footer = "\n[内容已截断]"
    if material_ids:
        footer = ("\n[内容已截断，可使用 material_get 读取完整素材；素材 ID："
                  + ", ".join(material_ids) + "]")
        if len(footer) > max_chars:
            # 用户预算小于素材身份本身时，整条历史不进入模型，不能产生失效的半个 ID。
            return ""
    elif len(footer) > max_chars:
        # 极小的调用方预算只能容纳正文切片；没有素材身份需要额外保留。
        footer = ""
    return content[:max_chars - len(footer)] + footer
