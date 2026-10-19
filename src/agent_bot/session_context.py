"""单会话一行的持久上下文；短事务合并接收事件、确认发送和有界摘要，无会话 TTL。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

import pymysql

from agent_bot.ai.database import query
from agent_bot.database import database_connector
from agent_bot.memory_store import ChatMemoryStoreError
from agent_bot.models import ChatMessage
from agent_bot.sent_messages import SentMessageUncertainError
from agent_bot.settings import DatabaseParams
from agent_bot.state import (
    ConversationContext,
    ConversationTurn,
    MemoryCompression,
    _fallback_memory,
    validate_request_id,
)

_T = TypeVar("_T")
_TABLE = "chat_session_context"
_MAX_CONTENT_CHARS = 1_032_000
_MODEL_MESSAGE_CHARS = 32_000


@dataclass(frozen=True, slots=True)
class SessionContextPolicy:
    """只控制模型窗口和摘要长度，不设置会话过期或会话数淘汰。"""

    history_turns: int
    max_history_chars: int
    memory_enabled: bool
    max_memory_chars: int

    def __post_init__(self) -> None:
        """策略无效时立即失败，不能静默回退到另一种会话保留规则。"""
        if self.history_turns < 0:
            raise ValueError("history turns cannot be negative")
        if self.max_history_chars <= 0 or self.max_memory_chars <= 0:
            raise ValueError("session context limits must be positive")


@dataclass(frozen=True, slots=True)
class ContextDelivery:
    """确认发送的不可变快照；历史已裁切时回执仍保留，正文为空。"""

    delivery_id: str
    request_id: str | None
    content: str
    material_ids: tuple[str, ...]
    created_at: datetime
    message_id: str | None = None


@dataclass(slots=True)
class _Session:
    """单次锁内读取的可变快照，不跨事务缓存消息或版本。"""

    session_id: str
    generation_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    memory: str = ""
    memory_revision: int = 0
    metadata: dict[str, Any] = field(default_factory=lambda: {
        "deliveries": {}, "requests": {}, "next_sequence": 0,
    })


class SessionContextStore:
    """每次写入在数据库锁内读新行再合并；普通消息不使用乐观版本锁。"""

    def __init__(
        self, params: DatabaseParams, *, policy: SessionContextPolicy,
        connect: Callable[..., Any] | None = None, clock: Callable[[], datetime] | None = None,
    ) -> None:
        """仅绑定已有数据库；新表由显式初始化或升级创建，普通调用不执行 DDL。"""
        self.params = params
        self.connect = connect or database_connector(params)
        self._policy = policy
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def policy(self) -> SessionContextPolicy:
        """暴露当前不可变策略，构建待发布快照时不得提前修改活动仓库。"""
        return self._policy

    @property
    def history_turns(self) -> int:
        """供调用方读取当前历史窗口，不影响发送幂等身份。"""
        return self._policy.history_turns

    @property
    def max_history_chars(self) -> int:
        """返回模型可见历史预算，完整素材仍由素材仓库保留。"""
        return self._policy.max_history_chars

    def update_policy(self, policy: SessionContextPolicy) -> None:
        """热刷新只更换后续操作策略，不删除会话或覆盖数据库当前内容。"""
        self._policy = policy

    def context(
        self, session_id: str, *, exclude_request_id: str | None = None,
    ) -> ConversationContext:
        """读取一致快照，仅排除当前请求已登记的用户事件，保留真实助手通知。"""
        _validate_session(session_id)
        validate_request_id(exclude_request_id)

        def read(connection: Any) -> ConversationContext:
            """只读当前一行，历史窗口裁切仅作用于此次模型副本。"""
            session = self._read(connection, session_id)
            if session is None:
                return ConversationContext(memory="", messages=())
            messages = []
            if self.history_turns:
                for event in sorted(session.messages, key=_event_order):
                    if event["role"] == "user" and (
                        exclude_request_id is not None
                        and event["request_id"] == exclude_request_id
                    ):
                        continue
                    content = self._preview(event)
                    if content:
                        messages.append(ChatMessage(
                            role=event["role"], content=content,
                            history_request_id=event["request_id"],
                        ))
                total = sum(len(message.content) for message in messages)
                while messages and (
                    len(messages) > self.history_turns * 2 or total > self.max_history_chars
                ):
                    total -= len(messages.pop(0).content)
            return ConversationContext(
                memory=session.memory if self._policy.memory_enabled else "",
                messages=tuple(messages), generation_id=session.generation_id,
            )

        return self._transaction(read)

    def ensure_generation(self, session_id: str) -> str:
        """确保会话行存在，任何时长后读取都不会自动更换代际。"""
        return self._merge(session_id, lambda session: session.generation_id)

    def record_received(
        self, session_id: str, content: str, *, request_id: str,
        created_at: datetime | None = None, generation_id: str | None = None,
    ) -> MemoryCompression | None:
        """模型执行前登记接收原文；相同请求保留首个时间，迟到事件按 UTC 排序。"""
        _validate_content(content)
        validate_request_id(request_id)
        if request_id is None:
            raise ValueError("received message requires a request id")
        if generation_id is not None:
            _validate_generation(generation_id)
        at = _utc(created_at) if created_at is not None else self._now()

        def append(session: _Session) -> MemoryCompression | None:
            """接收去重独立于消息窗口，裁切或清空不能恢复旧请求的写入权限。"""
            if generation_id is not None and generation_id != session.generation_id:
                return None
            receipt = session.metadata["requests"].get(request_id)
            identity = _digest(content)
            if receipt is not None:
                if receipt["user_hash"] != identity:
                    raise ValueError("received message identity conflicts")
                return None
            session.metadata["requests"][request_id] = {
                "generation_id": session.generation_id, "user_hash": identity,
                "committed": False, "active": True,
            }
            self._append(session, "user", content, request_id, at)
            return self._compact(session)

        return self._merge(session_id, append)

    def commit_exchange(
        self, session_id: str, user: str, assistant: str, *, request_id: str | None = None,
    ) -> MemoryCompression | None:
        """兼容旧轮次提交；已登记的输入不重复，真实发送存在时不伪造整段回答。"""
        validate_request_id(request_id)
        _validate_content(user)
        _validate_content(assistant)

        def append(session: _Session) -> MemoryCompression | None:
            """回执在窗口外仍有效，清空前已开始的请求不能借迟到提交污染新代际。"""
            receipts = session.metadata["requests"]
            receipt = receipts.get(request_id) if request_id is not None else None
            if receipt is not None and receipt["generation_id"] != session.generation_id:
                return None
            # 迁移可只恢复旧发送回执；缺少旧接收行也不能把稳定任务身份当作新提问。
            if request_id is not None and receipt is None and any(
                delivery["request_id"] == request_id
                and delivery["generation_id"] != session.generation_id
                for delivery in session.metadata["deliveries"].values()
            ):
                return None
            identity = _digest(user, assistant)
            if receipt is not None and receipt["committed"]:
                if receipt.get("commit_hash") != identity:
                    raise ValueError("conversation request identity conflicts")
                return None
            if receipt is None:
                receipt = {"generation_id": session.generation_id, "user_hash": _digest(user)}
                self._append(session, "user", user, request_id, self._now())
                if request_id is not None:
                    receipts[request_id] = receipt
            receipt.update(committed=True, commit_hash=identity, active=False)
            tracked_delivery = any(
                delivery["generation_id"] == session.generation_id
                and delivery["request_id"] == request_id
                for delivery in session.metadata["deliveries"].values()
            ) if request_id is not None else False
            # 请求已进入显式发送协议后，只能由平台确认落助手事件，未知占位也不能伪造成功。
            if not tracked_delivery:
                self._append(session, "assistant", assistant, request_id, self._now(),
                             origin="exchange")
            return self._compact(session)

        return self._merge(session_id, append)

    def apply_memory(self, compression: MemoryCompression, summary: str) -> bool:
        """只仲裁压缩结果的代际和快照；不因普通消息追加而拒绝它们或摘要。"""
        normalized = summary.strip()
        if not normalized or not self._policy.memory_enabled:
            return False

        def apply(session: _Session) -> bool:
            """相同快照只替换一次，后续压缩或清空会使旧结果明确失效。"""
            if (
                session.generation_id != compression.generation_id
                or session.memory_revision != compression.revision
                or session.metadata.get("compression_pending") != _compression_key(compression)
            ):
                return False
            session.memory = normalized[:self._policy.max_memory_chars]
            session.metadata.pop("compression_pending", None)
            return True

        return self._merge(compression.session_id, apply, create=False, missing=False)

    def clear(self, session_id: str) -> bool:
        """更换代际并清空消息摘要，保留全部发送和请求身份，禁止清空后重复发送。"""
        def reset(session: _Session) -> bool:
            """旧占位允许完成审计，但新代际不接收其助手事件。"""
            session.generation_id = uuid.uuid4().hex
            session.messages.clear()
            session.memory = ""
            session.memory_revision += 1
            session.metadata.pop("compression_pending", None)
            return True

        return self._merge(session_id, reset, create=False, missing=False)

    def compact(self, session_id: str) -> MemoryCompression | None:
        """独立通知或策略变化后收紧窗口，先同步落盘有界本地摘要供后续读取。"""
        return self._merge(session_id, self._compact, create=False)

    def finish_request(self, session_id: str, request_id: str) -> MemoryCompression | None:
        """成功或失败终态解除当前输入的压缩保护，不伪造成功回复或改变请求去重。"""
        validate_request_id(request_id)
        if request_id is None:
            raise ValueError("finished request requires a request id")

        def finish(session: _Session) -> MemoryCompression | None:
            """只标记已登记请求，旧代际的终态不能重新创建消息或摘要。"""
            receipt = session.metadata["requests"].get(request_id)
            if receipt is None:
                return None
            receipt["active"] = False
            return self._compact(session)

        return self._merge(session_id, finish, create=False)

    def claim_delivery(
        self, session_id: str, generation_id: str, delivery_id: str,
        request_id: str | None, message_id: str | None, content: str,
        material_ids: tuple[str, ...],
    ) -> ContextDelivery | None:
        """网络发送前占位；相同未确认身份禁止自动重发，完成身份直接复用回执。"""
        _validate_delivery(
            generation_id, delivery_id, request_id, message_id, content, material_ids,
        )

        def claim(session: _Session) -> ContextDelivery | None:
            """同一会话行锁仲裁发送者，事务内不等待平台网络。"""
            deliveries = session.metadata["deliveries"]
            identity = _delivery_identity(
                session_id, generation_id, request_id, content, material_ids,
            )
            existing = deliveries.get(delivery_id)
            if existing is not None:
                _check_delivery_identity(existing, identity)
                if existing["status"] != "sent":
                    raise SentMessageUncertainError("sent message delivery outcome is uncertain")
                return self._delivery(session, delivery_id)
            # 清空聊天不撤销此前授权的后台发送；确认时仍按原代际隔离模型上下文。
            deliveries[delivery_id] = {
                "generation_id": generation_id, "request_id": request_id,
                "message_id": message_id, "material_ids": list(material_ids),
                "identity": identity, "status": "pending", "content": content,
                "created_at": self._now().isoformat(timespec="microseconds"),
            }
            return None

        return self._merge(session_id, claim)

    def finish_delivery(
        self, session_id: str, delivery_id: str, message_id: str | None,
    ) -> ContextDelivery:
        """平台确认后落一次助手事件，旧代际的完成只更新回执。"""
        _validate_delivery_id(delivery_id)
        _validate_message_id(message_id)

        def finish(session: _Session) -> ContextDelivery:
            """确认和裁切在同一行事务提交，客户端重试不会追加第二份正文。"""
            delivery = session.metadata["deliveries"].get(delivery_id)
            if delivery is None:
                raise ValueError("sent message delivery was not claimed")
            if delivery["status"] == "sent":
                if delivery["message_id"] != message_id:
                    raise ValueError("sent message delivery receipt conflicts")
                return self._delivery(session, delivery_id)
            content = delivery.pop("content")
            delivery.update(status="sent", message_id=message_id,
                            created_at=self._now().isoformat(timespec="microseconds"))
            if delivery["generation_id"] == session.generation_id:
                self._remove_exchange(session, delivery["request_id"])
                self._append(
                    session, "assistant", content, delivery["request_id"],
                    datetime.fromisoformat(delivery["created_at"]),
                    material_ids=delivery["material_ids"], delivery_id=delivery_id,
                    message_id=message_id,
                )
                self._compact(session)
            return self._delivery(session, delivery_id)

        result = self._merge(session_id, finish, create=False)
        if result is None:
            raise ValueError("sent message delivery was not claimed")
        return result

    def record(
        self, session_id: str, generation_id: str, delivery_id: str,
        request_id: str | None, message_id: str | None, content: str,
        material_ids: tuple[str, ...],
    ) -> bool:
        """导入已确认发送；代际不符返回假，重复身份不追加消息。"""
        _validate_delivery(
            generation_id, delivery_id, request_id, message_id, content, material_ids,
        )

        def record(session: _Session) -> bool:
            """直接确认仍按正文身份去重，不能将未知占位伪装为发送成功。"""
            if generation_id != session.generation_id:
                return False
            deliveries = session.metadata["deliveries"]
            identity = _delivery_identity(
                session_id, generation_id, request_id, content, material_ids,
            )
            existing = deliveries.get(delivery_id)
            if existing is not None:
                _check_delivery_identity(existing, identity)
                if existing["status"] != "sent":
                    raise SentMessageUncertainError("sent message delivery outcome is uncertain")
                if existing["message_id"] != message_id:
                    raise ValueError("sent message delivery receipt conflicts")
                return False
            at = self._now()
            deliveries[delivery_id] = {
                "generation_id": generation_id, "request_id": request_id,
                "message_id": message_id, "material_ids": list(material_ids),
                "identity": identity, "status": "sent",
                "created_at": at.isoformat(timespec="microseconds"),
            }
            self._remove_exchange(session, request_id)
            self._append(session, "assistant", content, request_id, at,
                         material_ids=material_ids, delivery_id=delivery_id, message_id=message_id)
            self._compact(session)
            return True

        return self._merge(session_id, record, create=False, missing=False)

    def pending_deliveries(self, session_id: str, generation_id: str) -> tuple[str, ...]:
        """按原代际列未确认占位供已有平台回执对账，旧任务也绝不自动网络重发。"""
        _validate_session(session_id)
        _validate_generation(generation_id)

        def read(connection: Any) -> tuple[str, ...]:
            """保持占位插入顺序，旧代际可确认回执但不影响当前消息流。"""
            session = self._read(connection, session_id)
            if session is None:
                return ()
            return tuple(identity for identity, delivery in session.metadata["deliveries"].items()
                         if delivery["generation_id"] == generation_id
                         and delivery["status"] == "pending")

        return self._transaction(read)

    def list_messages(
        self, session_id: str, generation_id: str, limit: int = 100,
    ) -> tuple[ContextDelivery, ...]:
        """从统一消息流读取当前代际的确认发送，不再拼接独立出站表。"""
        _validate_session(session_id)
        _validate_generation(generation_id)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("sent message limit must be between 1 and 100")

        def read(connection: Any) -> tuple[ContextDelivery, ...]:
            """只返回仍在消息窗口中的完整正文，按事件时间及锁内顺序稳定排列。"""
            session = self._read(connection, session_id)
            if session is None or session.generation_id != generation_id:
                return ()
            events = [event for event in sorted(session.messages, key=_event_order)
                      if event.get("delivery_id")]
            return tuple(self._delivery(session, event["delivery_id"]) for event in events[-limit:])

        return self._transaction(read)

    def import_history(
        self, session_id: str, *, generation_id: str, memory_summary: str,
        events: Sequence[dict[str, Any]],
    ) -> bool:
        """显式迁移一次插入完整会话；已有新行不覆盖，坏事件不会留下半行历史。"""
        _validate_session(session_id)
        _validate_generation(generation_id)
        if not isinstance(memory_summary, str):
            raise ValueError("invalid session context memory")
        session = _Session(session_id, generation_id, memory=memory_summary)
        for event in events:
            role, content = event["role"], event["content"]
            if role not in {"user", "assistant"}:
                raise ValueError("invalid session context role")
            _validate_content(content)
            request_id = validate_request_id(event.get("request_id"))
            created_at = _utc(event["created_at"])
            materials = event.get("material_ids", ())
            if role == "user":
                if request_id is not None:
                    previous = session.metadata["requests"].get(request_id)
                    identity = _digest(content)
                    if previous is not None:
                        if previous["user_hash"] != identity:
                            raise ValueError("received message identity conflicts")
                        continue
                    session.metadata["requests"][request_id] = {
                        "generation_id": generation_id, "user_hash": identity, "committed": False,
                        "active": False,
                    }
                self._append(session, role, content, request_id, created_at, material_ids=materials)
                continue
            delivery_generation = event.get("generation_id", generation_id)
            delivery_id = event.get("delivery_id") or _digest(
                "legacy-assistant", session_id, delivery_generation, request_id,
                created_at.isoformat(timespec="microseconds"), content,
                session.metadata["next_sequence"],
            )
            message_id = event.get("message_id")
            _validate_delivery(
                delivery_generation, delivery_id, request_id, message_id, content, materials,
            )
            status = event.get("status", "sent")
            if status not in {"sent", "pending"}:
                raise ValueError("invalid session context delivery status")
            identity = _delivery_identity(
                session_id, delivery_generation, request_id, content, materials,
            )
            previous = session.metadata["deliveries"].get(delivery_id)
            if previous is not None:
                _check_delivery_identity(previous, identity)
                if previous["status"] != status or previous["message_id"] != message_id:
                    raise ValueError("sent message delivery receipt conflicts")
                continue
            delivery = {
                "generation_id": delivery_generation, "request_id": request_id,
                "message_id": message_id, "material_ids": list(materials),
                "identity": identity, "status": status,
                "created_at": created_at.isoformat(timespec="microseconds"),
            }
            session.metadata["deliveries"][delivery_id] = delivery
            if status == "pending":
                delivery["content"] = content
            elif delivery_generation == session.generation_id:
                self._append(session, role, content, request_id, created_at,
                             material_ids=materials, delivery_id=delivery_id, message_id=message_id)
        session.messages.sort(key=_event_order)
        self._compact(session)
        now = self._now()

        def insert(connection: Any) -> bool:
            """完整数据只经唯一键原子插入一次，迁移与普通首次写入不能相互覆盖。"""
            with connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT IGNORE INTO {_TABLE} (session_id,generation_id,messages_json,"
                    "memory_summary,memory_revision,deliveries_json,create_time,update_time) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (session_id, session.generation_id, _json(session.messages), session.memory,
                     session.memory_revision, _json(session.metadata), now, now),
                )
                return cursor.rowcount == 1

        return self._transaction(insert)

    def _merge(
        self, session_id: str, operation: Callable[[_Session], _T], *,
        create: bool = True, missing: Any = None,
    ) -> _T:
        """写前取得行锁，每次重新读合并；不把消息更新变成版本比较失败。"""
        _validate_session(session_id)

        def write(connection: Any) -> _T:
            """首次创建使用唯一键排他仲裁，避免 MySQL 读空行后升级锁死锁。"""
            if create:
                now = self._now()
                query(connection, f"INSERT INTO {_TABLE} (session_id,generation_id,messages_json,"
                      "memory_summary,memory_revision,deliveries_json,create_time,update_time) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON DUPLICATE KEY UPDATE "
                      "session_id=VALUES(session_id)",
                      (session_id, uuid.uuid4().hex, "[]", "", 0,
                       _json(_Session(session_id, "").metadata), now, now))
            session = self._read(connection, session_id, lock=True)
            if session is None:
                return missing
            result = operation(session)
            session.messages.sort(key=_event_order)
            query(connection, f"UPDATE {_TABLE} SET generation_id=%s,messages_json=%s,"
                  "memory_summary=%s,memory_revision=%s,deliveries_json=%s,update_time=%s "
                  "WHERE session_id=%s", (session.generation_id, _json(session.messages),
                  session.memory, session.memory_revision, _json(session.metadata),
                  self._now(), session_id))
            return result

        return self._transaction(write)

    def _read(self, connection: Any, session_id: str, *, lock: bool = False) -> _Session | None:
        """所有写分支在同一事务读取最新 JSON；数据损坏明确报错而不重建上下文。"""
        row = query(connection, f"SELECT * FROM {_TABLE} WHERE session_id=%s"
                    + (" FOR UPDATE" if lock else ""), (session_id,), one=True)
        if row is None:
            return None
        return _Session(session_id=row["session_id"], generation_id=row["generation_id"],
                        messages=json.loads(row["messages_json"]), memory=row["memory_summary"],
                        memory_revision=row["memory_revision"],
                        metadata=json.loads(row["deliveries_json"]))

    def _transaction(self, operation: Callable[[Any], _T]) -> _T:
        """SQLite BEGIN IMMEDIATE 与 MySQL 显式事务复用同一连接包装，失败完整回滚。"""
        try:
            with closing(self.connect(
                host=self.params.host, port=self.params.port, user=self.params.user,
                password=self.params.password, database=self.params.database, charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor, autocommit=False,
                connect_timeout=5, read_timeout=10, write_timeout=10,
            )) as connection:
                connection.begin()
                try:
                    result = operation(connection)
                    connection.commit()
                    return result
                except BaseException:
                    connection.rollback()
                    raise
        except pymysql.DatabaseError as error:
            raise ChatMemoryStoreError("session context database operation failed") from error

    def _now(self) -> datetime:
        """两种数据库均使用无时区 UTC 时间，模型排序不受本机时区影响。"""
        return _utc(self._clock())

    def _append(
        self, session: _Session, role: str, content: str, request_id: str | None,
        created_at: datetime, *, material_ids: Sequence[str] = (), **extra: Any,
    ) -> None:
        """锁内分配单调序号作为同一事件时间的确定次序，助手通知不虚构用户。"""
        sequence = session.metadata["next_sequence"]
        session.metadata["next_sequence"] = sequence + 1
        session.messages.append({
            "id": uuid.uuid4().hex, "role": role, "content": content, "request_id": request_id,
            "created_at": _utc(created_at).isoformat(timespec="microseconds"),
            "material_ids": list(material_ids), "sequence": sequence, **extra,
        })

    def _delivery(self, session: _Session, delivery_id: str) -> ContextDelivery:
        """已裁切正文不复制到回执；即使重置，身份与确认标识仍可阻止再次发送。"""
        delivery = session.metadata["deliveries"][delivery_id]
        content = next((event["content"] for event in session.messages
                        if event.get("delivery_id") == delivery_id), "")
        return ContextDelivery(
            delivery_id=delivery_id, request_id=delivery["request_id"], content=content,
            material_ids=tuple(delivery["material_ids"]),
            created_at=datetime.fromisoformat(delivery["created_at"]),
            message_id=delivery["message_id"],
        )

    @staticmethod
    def _remove_exchange(session: _Session, request_id: str | None) -> None:
        """真实分段到达时替换兼容整段，防止已发文本与模型摘要同时进入上下文。"""
        if request_id is not None:
            session.messages[:] = [event for event in session.messages
                                   if not (event.get("origin") == "exchange"
                                           and event["request_id"] == request_id)]

    def _preview(self, event: dict[str, Any]) -> str:
        """模型副本有界截断，素材标识必须完整保留才能读取原件。"""
        content = event["content"]
        maximum = min(_MODEL_MESSAGE_CHARS, self.max_history_chars)
        if len(content) <= maximum:
            return content
        footer = "\n[内容已截断]"
        if event["material_ids"]:
            footer = ("\n[内容已截断，可使用 material_get 读取完整素材；素材 ID："
                      + ", ".join(event["material_ids"]) + "]")
            if len(footer) > maximum:
                return ""
        elif len(footer) > maximum:
            footer = ""
        return content[:maximum - len(footer)] + footer

    def _compact(self, session: _Session) -> MemoryCompression | None:
        """只压缩结束请求的事件，所有并发活动输入组都不能提前进入摘要。"""
        session.messages.sort(key=_event_order)
        maximum = self.history_turns * 2
        total = sum(len(self._preview(event)) for event in session.messages)
        if len(session.messages) <= maximum and total <= self.max_history_chars:
            return None
        target = max(1, maximum // 2) if maximum else 0
        target_chars = max(1, self.max_history_chars // 2)
        if not self._policy.memory_enabled:
            target, target_chars = maximum, self.max_history_chars
        protected = {request_id for request_id, receipt in session.metadata["requests"].items()
                     if receipt.get("active") is True}
        evicted = []
        remaining = []
        count = len(session.messages)
        for event in session.messages:
            if event["request_id"] not in protected and (
                count > target or total > target_chars
            ):
                total -= len(self._preview(event))
                count -= 1
                evicted.append(event)
            else:
                remaining.append(event)
        session.messages = remaining
        if not evicted or not self._policy.memory_enabled:
            return None
        turns = []
        for event in evicted:
            if event["role"] == "user":
                turns.append(ConversationTurn(event["content"], "", event["request_id"]))
            elif turns and not turns[-1].assistant and turns[-1].request_id == event["request_id"]:
                user = turns.pop()
                turns.append(ConversationTurn(user.user, event["content"], event["request_id"]))
            else:
                turns.append(ConversationTurn("", event["content"], event["request_id"]))
        session.memory_revision += 1
        compression = MemoryCompression(
            session_id=session.session_id, revision=session.memory_revision,
            previous_memory=session.memory, turns=tuple(turns), generation_id=session.generation_id,
        )
        session.memory = _fallback_memory(compression, self._policy.max_memory_chars)
        session.metadata["compression_pending"] = _compression_key(compression)
        return compression


def _json(value: Any) -> str:
    """固定 JSON 编码，身份摘要不依赖 ASCII 转义或默认分隔空格。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _digest(*values: Any) -> str:
    """以明确结构计算身份，避免字符串拼接的分隔符碰撞。"""
    return hashlib.sha256(_json(values).encode("utf-8")).hexdigest()


def _delivery_identity(
    session_id: str, generation_id: str, request_id: str | None,
    content: str, material_ids: tuple[str, ...],
) -> str:
    """平台确认号独立于正文身份，允许占位的空号随后替换为真实回执。"""
    return _digest(session_id, generation_id, request_id, content, material_ids)


def _check_delivery_identity(delivery: dict[str, Any], identity: str) -> None:
    """已使用的幂等身份不能代表另一请求、正文、素材或代际。"""
    if delivery["identity"] != identity:
        raise ValueError("sent message delivery identity conflicts")


def _compression_key(compression: MemoryCompression) -> str:
    """只绑定摘要来源，不将普通追加消息当作版本冲突。"""
    return _digest(compression.previous_memory,
                   [(turn.user, turn.assistant, turn.request_id) for turn in compression.turns])


def _event_order(event: dict[str, Any]) -> tuple[str, int]:
    """UTC 时间是主序，序号仅仲裁同一微秒的消息。"""
    return event["created_at"], event["sequence"]


def _utc(value: datetime) -> datetime:
    """有时区时间转换为 UTC，无时区按数据库既有 UTC 契约解释。"""
    return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value


def _validate_session(session_id: str) -> None:
    """会话标识沿用持久库长度，不能隐式截断产生跨会话冲突。"""
    if not isinstance(session_id, str) or not session_id or len(session_id) > 128:
        raise ValueError("invalid session context session")


def _validate_generation(generation_id: str) -> None:
    """只接受系统生成的完整 UUID 十六进制代际。"""
    if not isinstance(generation_id, str) or not re.fullmatch(r"[a-f0-9]{32}", generation_id):
        raise ValueError("invalid sent message generation")


def _validate_delivery_id(delivery_id: str) -> None:
    """投递键是网关计算的 SHA256，不能被任意短键替代。"""
    if not isinstance(delivery_id, str) or not re.fullmatch(r"[a-f0-9]{64}", delivery_id):
        raise ValueError("invalid sent message delivery id")


def _validate_message_id(message_id: str | None) -> None:
    """平台回执允许无 ID，但已有 ID 不得为空或无限增长。"""
    if message_id is not None and (
        not isinstance(message_id, str) or not message_id or len(message_id) > 128
    ):
        raise ValueError("invalid sent message platform id")


def _validate_content(content: str) -> None:
    """与生成正文加素材引用的字符上限一致，不把中文或 emoji 当作字节拒绝。"""
    if not isinstance(content, str) or not content or len(content) > _MAX_CONTENT_CHARS:
        raise ValueError("invalid session context content")


def _validate_delivery(
    generation_id: str, delivery_id: str, request_id: str | None, message_id: str | None,
    content: str, material_ids: tuple[str, ...],
) -> None:
    """复用请求校验，素材只保留有界标识，不能解释为路径或自动读取文件。"""
    _validate_generation(generation_id)
    _validate_delivery_id(delivery_id)
    validate_request_id(request_id)
    _validate_message_id(message_id)
    _validate_content(content)
    if not isinstance(material_ids, tuple) or len(material_ids) > 100 or any(
        not isinstance(value, str) or not value or len(value) > 128 for value in material_ids
    ):
        raise ValueError("invalid sent message materials")
