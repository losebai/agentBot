"""有界事务内维护当前事实、来源、不可变版本和批量整理。"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from agent_bot.ai.contracts import Page
from agent_bot.ai.database import page_limit
from agent_bot.knowledge.contracts import (
    MemoryCreate,
    MemoryRevisionView,
    MemoryView,
    ScopeView,
    TagCount,
)
from agent_bot.knowledge.errors import KnowledgeError
from agent_bot.knowledge.pagination import decode_cursor, encode_cursor, literal_like
from agent_bot.memory.contracts import (
    MemoryCleanup,
    MemoryCleanupPreview,
    MemoryEvidence,
    MemoryMerge,
    MemoryResolve,
    MemoryStatistics,
    MemoryTarget,
    MemoryUpdate,
)
from agent_bot.memory.policy import (
    IMPORTANCE,
    effective_status,
    fingerprint,
    normalized_content,
    preview_digest,
    time_value,
    utc_now,
)
from agent_bot.services.tagged_memory import (
    MemoryArguments,
    MemoryIdentity,
    MemoryRecord,
    MemoryResult,
    MemorySourceError,
    MemoryTag,
    memory_scope_owner,
    normalize_memory_tags,
)


def _conflict(message: str) -> None:
    """固定业务异常复用既有HTTP映射，不泄露数据库信息。"""
    from agent_bot.integrations.tagged_memory_store import TaggedMemoryConflictError

    raise TaggedMemoryConflictError(message)


def _json(value: Any) -> str:
    """时间与中文统一序列化，快照不是任意可执行资料。"""
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _public_time(value: Any) -> datetime | None:
    """明确的新时间统一带UTC；旧时间另外保留来源标识。"""
    parsed = time_value(value)
    return parsed.replace(tzinfo=timezone.utc) if parsed is not None else None


def _evidence_key(source_kind: str | None, source_ref: str | None, excerpt: str) -> str:
    """同一个真实事件只有一个来源身份，换片段或称呼不能增加独立佐证。"""
    return fingerprint(_json(("event", source_ref) if source_ref else (source_kind, None, excerpt)))


def _expiration_changes(row: dict[str, Any]) -> dict[str, Any]:
    """未来约定提前撤销只改状态，不能伪造结束早于开始的时间区间。"""
    now = utc_now()
    start = time_value(row["valid_from"])
    return {"status": "expired", **({"valid_until": now} if start is None or start < now else {})}


_ACTIVE_SQL = (
    "is_deleted=0 AND status='active' "
    "AND (valid_from IS NULL OR valid_from<=%s) "
    "AND (valid_until IS NULL OR valid_until>%s)"
)


def _occupies_capacity(row: dict[str, Any], now: datetime) -> bool:
    """未来生效的事实也预留名额，已到期和退出当前集合的事实不占用。"""
    until = time_value(row.get("valid_until"))
    return (
        not row.get("is_deleted")
        and row["status"] in {"active", "pending", "disputed"}
        and (until is None or until > now)
    )


class MemoryLifecycleMixin:
    """复用存储连接与作用域锁；所有网络调用必须在本混入类外完成。"""

    def _row(self, cursor: Any, id: int, *, deleted: bool = False) -> dict:
        """管理审计可显式读墓碑，普通详情不重新开放遗忘正文。"""
        cursor.execute("SELECT * FROM chat_tagged_memory WHERE id=%s", (id,))
        row = cursor.fetchone()
        if row is None or (row["is_deleted"] and not deleted):
            raise KnowledgeError("MEMORY_NOT_FOUND", status_code=404)
        return row

    def _operation_result(
        self, cursor: Any, owner: str, identity: MemoryIdentity | None, arguments: MemoryArguments
    ) -> MemoryResult | None:
        """以受信来源和操作身份读取成功回执，已失效正文不从回执重新开放。"""
        request = identity.source_event_id
        if not request:
            return None
        digest = hashlib.sha256(arguments.model_dump_json().encode()).hexdigest()
        operation = arguments.operation_id or digest
        cursor.execute(
            "SELECT arguments_digest,result_json FROM chat_memory_operation "
            "WHERE scope_id=%s AND request_id=%s AND operation_id=%s",
            (owner, request, operation),
        )
        receipt = cursor.fetchone()
        if receipt is None:
            return None
        if receipt["arguments_digest"] != digest:
            _conflict("同一记忆操作身份对应不同参数")
        payload = json.loads(receipt["result_json"])
        records = []
        for ref in payload["records"]:
            cursor.execute("SELECT * FROM chat_tagged_memory WHERE id=%s", (ref["id"],))
            row = cursor.fetchone()
            if (
                row
                and row["scope_id"] == owner
                and not row["is_deleted"]
                and row["revision"] == ref["revision"]
            ):
                records.append(self._record(cursor, row, identity))
        return MemoryResult(
            status=payload["status"],
            records=tuple(records),
            snapshot_changed=len(records) != len(payload["records"]),
        )

    def _remember_operation(
        self,
        cursor: Any,
        owner: str,
        identity: MemoryIdentity | None,
        arguments: MemoryArguments,
        result: Any,
    ) -> None:
        """业务变更、空操作与成功回执同时提交，回执不复制正文。"""
        if not identity.source_event_id:
            return
        digest = hashlib.sha256(arguments.model_dump_json().encode()).hexdigest()
        payload = {
            "status": result.status,
            "records": [{"id": row.id, "revision": row.revision} for row in result.records],
        }
        cursor.execute(
            "INSERT INTO chat_memory_operation "
            "(scope_id,request_id,operation_id,arguments_digest,"
            "operation_kind,result_json,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                owner,
                identity.source_event_id,
                arguments.operation_id or digest,
                digest,
                arguments.action,
                _json(payload),
                utc_now(),
            ),
        )

    def _remember_recall(
        self,
        cursor: Any,
        identity: MemoryIdentity | None,
        records: list[MemoryRecord] | tuple[MemoryRecord, ...],
    ) -> None:
        """计一次已完成召回，再登记返回版本供投递后验引用；不保存查询与正文。"""
        if not identity or not identity.request_id:
            return
        # 空命中也计入分母；一次返回多条事实仍只生成一个调用计数。
        operation = f"recall-attempt-{uuid.uuid4().hex}"
        cursor.execute(
            "INSERT INTO chat_memory_operation "
            "(scope_id,request_id,operation_id,arguments_digest,"
            "operation_kind,result_json,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                identity.owner("session", self._secret), identity.request_id,
                operation, fingerprint(operation), "recall_attempt",
                _json({"hit_count": len(records)}), utc_now(),
            ),
        )
        for record in records:
            owner = identity.owner(record.scope, self._secret)
            operation = f"recall-{record.id}-{record.revision}"
            cursor.execute(
                "SELECT operation_id FROM chat_memory_operation WHERE scope_id=%s "
                "AND request_id=%s AND operation_id=%s",
                (owner, identity.request_id, operation),
            )
            if cursor.fetchone():
                continue
            payload = {
                "id": record.id,
                "revision": record.revision,
                "session_id": identity.session_id,
            }
            cursor.execute(
                "INSERT INTO chat_memory_operation "
                "(scope_id,request_id,operation_id,arguments_digest,"
                "operation_kind,result_json,created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    owner,
                    identity.request_id,
                    operation,
                    fingerprint(operation),
                    "recall",
                    _json(payload),
                    utc_now(),
                ),
            )

    def record_adoption(self, session_id: str, request_id: str | None, content: str) -> None:
        """只有平台确认正文引用本轮召回版本才加热，重试投递不重复计数。"""
        if not request_id:
            return
        references = {
            (int(id), int(version))
            for id, version in re.findall(
                r"【记忆 #(\d+) v(\d+)】",
                content,
            )
        }
        if not references:
            return
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT scope_id,result_json FROM chat_memory_operation "
                "WHERE request_id=%s AND operation_kind='recall'",
                (request_id,),
            )
            recalls = cursor.fetchall()
        for receipt in recalls:
            ref = json.loads(receipt["result_json"])
            if ref["session_id"] != session_id or (ref["id"], ref["revision"]) not in references:
                continue
            owner = receipt["scope_id"]
            with self._transaction(owner) as cursor:
                row = self._row(cursor, ref["id"], deleted=True)
                if (
                    row["scope_id"] != owner
                    or row["revision"] != ref["revision"]
                    or effective_status(row) != "active"
                ):
                    continue
                operation = f"use-{row['id']}-{row['revision']}"
                cursor.execute(
                    "SELECT operation_id FROM chat_memory_operation WHERE scope_id=%s "
                    "AND request_id=%s AND operation_id=%s",
                    (owner, request_id, operation),
                )
                if cursor.fetchone():
                    continue
                cursor.execute(
                    "UPDATE chat_tagged_memory SET use_count=use_count+1,last_used_at=%s "
                    "WHERE id=%s AND revision=%s",
                    (utc_now(), row["id"], row["revision"]),
                )
                cursor.execute(
                    "INSERT INTO chat_memory_operation "
                    "(scope_id,request_id,operation_id,arguments_digest,"
                    "operation_kind,result_json,created_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                    (
                        owner,
                        request_id,
                        operation,
                        fingerprint(operation),
                        "touch",
                        "{}",
                        utc_now(),
                    ),
                )

    def _sources(self, cursor: Any, id: int) -> tuple[MemoryEvidence, ...]:
        """展示最近的有界来源，全部历史证据仍在数据库中保留。"""
        cursor.execute(
            "SELECT id,source_kind,source_ref,source_time,excerpt "
            "FROM chat_memory_evidence WHERE memory_id=%s ORDER BY id DESC LIMIT 32",
            (id,),
        )
        return tuple(
            MemoryEvidence(**{**row, "source_time": _public_time(row["source_time"])})
            for row in cursor.fetchall()
        )

    def _admin_views(self, cursor: Any, rows: list[dict]) -> tuple[MemoryView, ...]:
        """当前快照附有界证据和有效状态，不从昵称反推身份。"""
        return tuple(
            MemoryView(
                id=row["id"],
                scope_id=row["scope_id"],
                scope=row["scope"],
                key=row["memory_key"],
                content=row["content"],
                tags=tuple(json.loads(row["tags_json"])),
                material_ids=tuple(json.loads(row["material_ids_json"])),
                created_at=(
                    _public_time(row["create_time"])
                    if row["created_time_basis"] == "utc"
                    else row["create_time"]
                ),
                updated_at=(
                    _public_time(row["update_time"])
                    if row["updated_time_basis"] == "utc"
                    else row["update_time"]
                ),
                revision=row["revision"],
                topic=row["topic"],
                kind=row["kind"],
                status=row["status"],
                effective_status=effective_status(row),
                subject_type=row["subject_type"],
                subject_id=row["subject_id"],
                source_kind=row["source_kind"],
                sources=self._sources(cursor, row["id"]),
                importance=row["importance"],
                observed_at=_public_time(row["observed_at"]),
                valid_from=_public_time(row["valid_from"]),
                valid_until=_public_time(row["valid_until"]),
                last_used_at=_public_time(row["last_used_at"]),
                task_state=row["task_state"],
                merged_into_id=row["merged_into_id"],
                conflict_set_id=row["conflict_set_id"],
                created_time_basis=row["created_time_basis"],
                updated_time_basis=row["updated_time_basis"],
            )
            for row in rows
        )

    def _admin_get(self, cursor: Any, id: int) -> MemoryView:
        """读取当前未遗忘事实；历史入口另外要求管理员权限。"""
        return self._admin_views(cursor, [self._row(cursor, id)])[0]

    def _snapshot(self, cursor: Any, row: dict, operation: str, reason: str, actor: str) -> None:
        """当前写入与不可变历史共用事务，不覆盖既有版本。"""
        view = self._admin_views(cursor, [row])[0]
        cursor.execute(
            "INSERT INTO chat_memory_revision "
            "(memory_id,revision,operation,snapshot_json,actor_type,reason,created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                row["id"],
                row["revision"],
                operation,
                view.model_dump_json(),
                actor,
                reason,
                utc_now(),
            ),
        )

    def _evidence(
        self,
        cursor: Any,
        row: dict,
        *,
        identity: MemoryIdentity | None = None,
        excerpt: str = "",
        source_kind: str = "legacy_unknown",
    ) -> bool:
        """真实来源去重，引用保留原作者；同来源重放不能刷佐证数量或确认时间。"""
        source_ref = identity.source_event_id if identity else None
        signature = _evidence_key(source_kind, source_ref, excerpt)
        cursor.execute(
            "SELECT id FROM chat_memory_evidence WHERE memory_id=%s AND evidence_fingerprint=%s",
            (row["id"], signature),
        )
        if cursor.fetchone():
            return False
        actor = None
        if identity:
            actor_id = (identity.source_user_id if source_kind == "third_party"
                        else identity.user_id)
            if actor_id is not None:
                actor = memory_scope_owner(identity.bot_id, "user", actor_id, self._secret)
        cursor.execute(
            "INSERT INTO chat_memory_evidence "
            "(memory_id,first_revision,source_kind,source_ref,source_actor_id,"
            "source_session_id,source_event_id,source_time,recorded_at,excerpt,"
            "evidence_fingerprint,original_memory_id) VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                row["id"],
                row["revision"],
                source_kind,
                source_ref,
                actor,
                identity.session_id if identity else None,
                source_ref,
                time_value(identity.source_time) if identity else None,
                utc_now(),
                excerpt,
                signature,
                None,
            ),
        )
        return True

    def _version(self, row: dict, expected: int | None) -> None:
        """内部兼容读取不要求版本，任何明确修订必须核对版本。"""
        if expected is not None and row["revision"] != expected:
            _conflict("记忆版本已变化，请重新读取后提交")

    def _capacity(self, cursor: Any, owner: str, *, excluding: int | None = None) -> None:
        """未来生效事实占名额；历史、墓碑和已到期事实不占当前名额。"""
        cursor.execute(
            "SELECT COUNT(*) AS memory_count FROM chat_tagged_memory "
            "WHERE scope_id=%s AND is_deleted=0 AND status IN ('active','pending','disputed') "
            "AND (valid_until IS NULL OR valid_until>%s)"
            + (" AND id<>%s" if excluding is not None else ""),
            (owner, utc_now(), *((excluding,) if excluding is not None else ())),
        )
        count = cursor.fetchone()["memory_count"]
        if count >= 256:
            from agent_bot.integrations.tagged_memory_store import TaggedMemoryCapacityError

            raise TaggedMemoryCapacityError("此作用域当前记忆已满，请明确整理旧事实")

    def _change(
        self,
        cursor: Any,
        row: dict,
        changes: dict,
        *,
        operation: str,
        reason: str,
        actor: str = "admin",
        identity: MemoryIdentity | None = None,
        excerpt: str = "",
    ) -> dict:
        """只写显式编辑字段，并在同事务生成含来源的新版本。"""
        allowed = {
            "content",
            "tags_json",
            "topic",
            "kind",
            "status",
            "importance",
            "observed_at",
            "valid_from",
            "valid_until",
            "task_state",
            "merged_into_id",
            "conflict_set_id",
            "source_kind",
            "is_deleted",
            "subject_type",
            "subject_id",
        }
        if not set(changes) <= allowed:
            raise ValueError("invalid memory change")
        kind = changes.get("kind", row["kind"])
        task_state = changes.get("task_state", row["task_state"])
        if kind != "task" and task_state is not None:
            if "task_state" in changes:
                raise ValueError("只有任务可以设置完成状态")
            changes["task_state"] = None
        if kind == "task" and task_state in {"completed", "cancelled"}:
            changes["importance"] = 0.5
        if "content" in changes:
            changes["content_fingerprint"] = fingerprint(changes["content"])
            cursor.execute(
                "SELECT * FROM chat_tagged_memory WHERE scope_id=%s AND "
                "content_fingerprint=%s AND id<>%s AND status<>'merged'",
                (row["scope_id"], changes["content_fingerprint"], row["id"]),
            )
            for other in cursor.fetchall():
                if other["subject_id"] == row["subject_id"] and normalized_content(
                    other["content"]
                ) == normalized_content(changes["content"]):
                    _conflict("已存在同主体事实，请明确合并，不能通过编辑制造重复")
        start = time_value(changes.get("valid_from", row["valid_from"]))
        until = time_value(changes.get("valid_until", row["valid_until"]))
        if start and until and until <= start:
            raise ValueError("invalid memory validity interval")
        now = utc_now()
        if not _occupies_capacity(row, now) and _occupies_capacity({**row, **changes}, now):
            self._capacity(cursor, row["scope_id"], excluding=row["id"])
        changes.update(
            revision=row["revision"] + 1, update_time=utc_now(), updated_time_basis="utc"
        )
        cursor.execute(
            "UPDATE chat_tagged_memory SET "
            + ",".join(f"{key}=%s" for key in changes)
            + " WHERE id=%s AND revision=%s",
            (*changes.values(), row["id"], row["revision"]),
        )
        changed = {**row, **changes}
        if identity is not None or actor == "admin":
            self._evidence(
                cursor,
                changed,
                identity=identity,
                excerpt=excerpt,
                source_kind=changes.get("source_kind", "admin_confirmation"),
            )
        self._snapshot(cursor, changed, operation, reason, actor)
        return changed

    def _confirm(
        self,
        cursor: Any,
        row: dict[str, Any],
        identity: MemoryIdentity | None,
        excerpt: str,
        source_kind: str | None,
    ) -> tuple[dict[str, Any], bool]:
        """只有新独立真实来源才生成确认版本，同源重放保持空操作。"""
        if not identity or not identity.source_event_id:
            return row, False
        signature = _evidence_key(source_kind, identity.source_event_id, excerpt)
        cursor.execute(
            "SELECT id FROM chat_memory_evidence WHERE memory_id=%s AND evidence_fingerprint=%s",
            (row["id"], signature),
        )
        if cursor.fetchone():
            return row, False
        changed = self._change(
            cursor,
            row,
            {"source_kind": source_kind},
            operation="confirm",
            reason="同一事实获得新的独立来源确认",
            actor="user",
            identity=identity,
            excerpt=excerpt,
        )
        return changed, True

    def _write(
        self,
        cursor: Any,
        owner: str,
        scope: str | None,
        arguments: MemoryArguments,
        *,
        identity: MemoryIdentity | None = None,
        source_kind: str | None = None,
    ) -> MemoryResult:
        """保存精确去重；更正版本化；遗忘保留墓碑，永不换键绕过冲突。"""
        if arguments.action not in {"save", "forget"}:
            return self._tool_revision(cursor, owner, arguments, identity)
        cursor.execute(
            "SELECT * FROM chat_tagged_memory WHERE scope_id=%s AND "
            + ("id=%s" if arguments.id else "memory_key=%s"),
            (owner, arguments.id or arguments.key),
        )
        existing = cursor.fetchone()
        if arguments.action == "forget":
            if not existing or existing["is_deleted"]:
                return MemoryResult(status="not_found")
            self._version(existing, arguments.expected_revision)
            self._change(
                cursor,
                existing,
                {"status": "deleted", "is_deleted": 1},
                operation="forget",
                reason=arguments.reason or "用户明确要求遗忘",
                actor="user",
                identity=identity,
            )
            return MemoryResult(status="forgotten")
        subject_type = "user" if scope == "user" else ("shared" if scope == "global" else scope)
        subject = identity.owner("user", self._secret) if scope == "user" and identity else owner
        if identity and identity.source_text is not None:
            if not arguments.source_excerpt or arguments.source_excerpt not in identity.source_text:
                raise ValueError("记忆来源必须属于当前真实消息")
            if arguments.kind in {"preference", "relationship"} and scope != "user":
                raise ValueError("个人偏好和关系只能保存至本人用户作用域")
        source_kind = source_kind or (
            identity.source_kind if identity and identity.source_text else "legacy_unknown"
        )
        if existing:
            # 模型声明 fact 不能让同键确认绕过已存分类，把引用挂到本人的偏好或约定。
            if source_kind == "third_party" and existing["kind"] != "fact":
                raise MemorySourceError()
            if existing["is_deleted"]:
                _conflict("记忆键已存在且该事实已遗忘，不能恢复")
            if existing["status"] == "merged":
                _conflict("该事实已合并，请读取合并目标的当前版本")
            if normalized_content(existing["content"]) != normalized_content(arguments.content):
                _conflict("记忆键已存在：更正需使用 revise 和当前版本，不能换键新增")
        cursor.execute(
            "SELECT * FROM chat_tagged_memory WHERE scope_id=%s AND content_fingerprint=%s",
            (owner, fingerprint(arguments.content)),
        )
        duplicates = [
            row
            for row in cursor.fetchall()
            if row["subject_id"] in {subject, ""}
            and normalized_content(row["content"]) == normalized_content(arguments.content)
        ]
        # 先检查全部分类墓碑，不能让较早命中的活事实或同键返回绕过遗忘。
        if any(row["is_deleted"] for row in duplicates):
            _conflict("该事实已遗忘，不能通过换键恢复")
        if existing:
            existing, confirmed = self._confirm(
                cursor, existing, identity, arguments.source_excerpt or "", source_kind
            )
            return MemoryResult(
                status="confirmed" if confirmed else "unchanged",
                records=(self._record(cursor, existing, identity),),
            )
        for duplicate in duplicates:
            if duplicate["status"] != "merged" and duplicate["kind"] == arguments.kind:
                duplicate, confirmed = self._confirm(
                    cursor, duplicate, identity, arguments.source_excerpt or "", source_kind
                )
                return MemoryResult(
                    status="confirmed" if confirmed else "deduplicated",
                    records=(self._record(cursor, duplicate, identity),),
                )
        if any(row["status"] == "merged" for row in duplicates):
            _conflict("该事实已合并，请读取合并目标的当前版本")
        self._capacity(cursor, owner)
        now = utc_now()
        topic = normalized_content(arguments.topic or arguments.tags[0]).casefold()
        values = dict(
            scope_id=owner,
            memory_key=arguments.key,
            content=arguments.content,
            create_time=now,
            update_time=now,
            scope=scope,
            tags_json=_json(sorted(arguments.tags)),
            material_ids_json=_json(arguments.material_ids),
            revision=1,
            topic=topic,
            kind=arguments.kind,
            status="active",
            subject_type=subject_type,
            subject_id=subject,
            source_kind=source_kind,
            importance=IMPORTANCE[arguments.kind],
            content_fingerprint=fingerprint(arguments.content),
            observed_at=time_value(arguments.observed_at),
            valid_from=time_value(arguments.valid_from),
            valid_until=time_value(arguments.valid_until),
            task_state=(arguments.task_state or "open") if arguments.kind == "task" else None,
        )
        cursor.execute(
            "INSERT INTO chat_tagged_memory ("
            + ",".join(values)
            + ") VALUES ("
            + ",".join("%s" for _ in values)
            + ")",
            tuple(values.values()),
        )
        row = self._row(cursor, cursor.lastrowid)
        self._evidence(
            cursor,
            row,
            identity=identity,
            excerpt=arguments.source_excerpt or "",
            source_kind=source_kind,
        )
        self._snapshot(
            cursor,
            row,
            "create",
            arguments.reason or "保存跨会话可用事实",
            "admin" if source_kind == "admin_confirmation" else "user",
        )
        return MemoryResult(status="saved", records=(self._record(cursor, row, identity),))

    def _tool_revision(
        self, cursor: Any, owner: str, arguments: MemoryArguments, identity: MemoryIdentity | None
    ) -> MemoryResult:
        """QQ只能更正本人事实；共享资料的不同结论进入待确认冲突。"""
        row = self._row(cursor, arguments.id)
        if row["scope_id"] != owner:
            raise KnowledgeError("MEMORY_NOT_FOUND", status_code=404)
        self._version(row, arguments.expected_revision)
        if row["status"] in {"merged", "deleted"}:
            _conflict("已合并或遗忘的事实不能恢复")
        if arguments.action == "merge":
            # 自动语义相似不等价于同一事实，管理端明确预览后才允许合并。
            return MemoryResult(status="needs_review")
        if arguments.action == "expire":
            changed = self._change(
                cursor,
                row,
                _expiration_changes(row),
                operation="expire",
                reason=arguments.reason,
                actor="user",
                identity=identity,
            )
            return MemoryResult(
                status="expired", records=(self._record(cursor, changed, identity),)
            )
        if row["scope"] in {"global", "group"} and identity and identity.source_text:
            return self._propose_conflict(cursor, row, arguments, identity)
        if (
            not identity
            or row["subject_type"] != "user"
            or row["subject_id"] != identity.owner("user", self._secret)
        ):
            return MemoryResult(status="needs_review")
        if arguments.retention_reason != "correction":
            return MemoryResult(status="needs_review")
        changes = {
            "content": arguments.content,
            "source_kind": "user_correction",
            "importance": 1.0,
            "status": "active",
        }
        if arguments.task_state is not None:
            changes["task_state"] = arguments.task_state
        if arguments.observed_at is not None:
            changes["observed_at"] = time_value(arguments.observed_at)
        for name in ("valid_from", "valid_until"):
            if getattr(arguments, name) is not None:
                changes[name] = time_value(getattr(arguments, name))
        if arguments.tags:
            changes["tags_json"] = _json(arguments.tags)
        changed = self._change(
            cursor,
            row,
            changes,
            operation="revise",
            reason=arguments.reason,
            actor="user",
            identity=identity,
            excerpt=arguments.source_excerpt or "",
        )
        return MemoryResult(status="revised", records=(self._record(cursor, changed, identity),))

    def _propose_conflict(
        self,
        cursor: Any,
        row: dict[str, Any],
        arguments: MemoryArguments,
        identity: MemoryIdentity | None,
    ) -> MemoryResult:
        """新共享报告保留为分支，弱于原证据时不使原事实失效。"""
        if not arguments.source_excerpt or arguments.source_excerpt not in identity.source_text:
            raise ValueError("冲突证据不属于当前消息")
        self._capacity(cursor, row["scope_id"])
        group = row["conflict_set_id"]
        if group is None:
            cursor.execute(
                "SELECT conflict_set_id FROM chat_tagged_memory WHERE scope_id=%s "
                "AND memory_key LIKE %s AND conflict_set_id IS NOT NULL "
                "AND status IN ('pending','disputed') ORDER BY id LIMIT 1",
                (row["scope_id"], f"conflict-{row['id']}-%"),
            )
            previous = cursor.fetchone()
            group = previous["conflict_set_id"] if previous else uuid.uuid4().hex
        branch_suffix = fingerprint(_json((identity.source_event_id, arguments.content)))[:16]
        branch_key = f"conflict-{row['id']}-{branch_suffix}"
        created = self._write(
            cursor,
            row["scope_id"],
            row["scope"],
            MemoryArguments(
                action="save",
                scope=row["scope"],
                key=branch_key,
                content=arguments.content,
                tags=tuple(json.loads(row["tags_json"])),
                topic=row["topic"],
                kind=row["kind"],
                source_excerpt=arguments.source_excerpt,
                retention_reason="reusable_fact",
            ),
            identity=identity,
        )
        if created.status != "saved":
            return created
        branch = self._row(cursor, created.records[0].id)
        comparable = row["source_kind"] == "self_report"
        status = "disputed" if comparable else "pending"
        if comparable:
            self._change(
                cursor,
                row,
                {"status": status, "conflict_set_id": group},
                operation="dispute",
                reason=arguments.reason,
                actor="user",
            )
        changed = self._change(
            cursor,
            branch,
            {"status": status, "conflict_set_id": group},
            operation="dispute",
            reason=arguments.reason,
            actor="user",
        )
        return MemoryResult(status=status, records=(self._record(cursor, changed, identity),))

    def _record(
        self, cursor: Any, row: dict, identity: MemoryIdentity | None = None
    ) -> MemoryRecord:
        """共享记忆只共享文本，素材仍须匹配当前来源会话。"""
        material_ids = tuple(json.loads(row["material_ids_json"]))
        if material_ids and identity is not None:
            cursor.execute(
                "SELECT id FROM chat_material WHERE entry='qq' AND session_id=%s "
                "AND id IN (" + ",".join("%s" for _ in material_ids) + ")",
                (identity.session_id, *material_ids),
            )
            visible = {item["id"] for item in cursor.fetchall()}
            material_ids = tuple(item for item in material_ids if item in visible)
        view = self._admin_views(cursor, [row])[0]
        data = view.model_dump(include=set(MemoryRecord.model_fields))
        data["material_ids"] = material_ids
        return MemoryRecord(**data)

    def read_candidates(self, identity: MemoryIdentity, arguments: MemoryArguments) -> list[dict]:
        """只归还身份授权且当前有效的候选，供事务外计算向量。"""
        scopes = identity.scopes if arguments.scope == "all" else (arguments.scope,)
        owners = tuple(identity.owner(scope, self._secret) for scope in scopes)
        now = utc_now()
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT * FROM chat_tagged_memory WHERE scope_id IN ("
                + ",".join("%s" for _ in owners)
                + ") AND "
                + _ACTIVE_SQL,
                (*owners, now, now),
            )
            return [
                {**row, "tags": tuple(json.loads(row["tags_json"]))}
                for row in cursor.fetchall()
                if effective_status(row) == "active"
            ]

    def semantic_result(self, identity: MemoryIdentity | None, rows: list[dict]) -> MemoryResult:
        """生成结果前重新校验权限和版本，遗忘竞争不能泄露旧快照。"""
        records = []
        owners = {identity.owner(scope, self._secret) for scope in identity.scopes}
        with self._transaction() as cursor:
            for proposed in rows[:8]:
                cursor.execute("SELECT * FROM chat_tagged_memory WHERE id=%s", (proposed["id"],))
                row = cursor.fetchone()
                if (
                    row is not None
                    and row["scope_id"] in owners
                    and not row["is_deleted"]
                    and row["revision"] == proposed["revision"]
                    and effective_status(row) == "active"
                ):
                    records.append(
                        self._record(cursor, row, identity).model_copy(
                            update={"relevance": proposed.get("relevance")}
                        )
                    )
            self._remember_recall(cursor, identity, records)
        return MemoryResult(
            status="recalled",
            records=tuple(records),
            mode="semantic",
            snapshot_changed=len(records) != len(rows[:8]),
        )

    def _recall(
        self,
        cursor: Any,
        owners: tuple[str, ...],
        tags: tuple[str, ...],
        session_id: str,
        *,
        identity: MemoryIdentity | None = None,
    ) -> MemoryResult:
        """显式标签检索也过滤时间与状态，不伪装为向量模式。"""
        now = utc_now()
        cursor.execute(
            "SELECT * FROM chat_tagged_memory WHERE scope_id IN ("
            + ",".join("%s" for _ in owners)
            + ") AND "
            + _ACTIVE_SQL,
            (*owners, now, now),
        )
        rows = [row for row in cursor.fetchall() if effective_status(row) == "active"]
        for row in rows:
            row["matches"] = len(set(tags).intersection(json.loads(row["tags_json"])))
        rows = [row for row in rows if row["matches"]]
        rows.sort(key=lambda row: (row["matches"], row["importance"], row["id"]), reverse=True)
        bound = identity or MemoryIdentity(
            bot_id="internal", user_id="internal", session_id=session_id
        )
        records = tuple(self._record(cursor, row, bound) for row in rows[:8])
        self._remember_recall(cursor, identity, records)
        return MemoryResult(status="recalled", records=records)

    def _tags(self, cursor: Any, owners: tuple[str, ...], offset: int) -> MemoryResult:
        """仅有效事实形成主题目录，历史与墓碑不再抢占标签。"""
        now = utc_now()
        cursor.execute(
            "SELECT * FROM chat_tagged_memory WHERE scope_id IN ("
            + ",".join("%s" for _ in owners)
            + ") AND "
            + _ACTIVE_SQL,
            (*owners, now, now),
        )
        keys = sorted(
            {
                (row["scope"], tag)
                for row in cursor.fetchall()
                if effective_status(row) == "active"
                for tag in json.loads(row["tags_json"])
            }
        )
        page = keys[offset : offset + 65]
        return MemoryResult(
            status="tags",
            tags=tuple(MemoryTag(scope=scope, tag=tag) for scope, tag in page[:64]),
            next_offset=offset + 64 if len(page) > 64 else None,
        )

    def admin_create(self, body: MemoryCreate) -> MemoryView:
        """管理员指定明确目标，来源标为管理员录入而非本人自述。"""
        owner = body.scope_id or memory_scope_owner(
            body.bot_id, body.scope, body.target or "", self._secret
        )
        with self._transaction(owner) as cursor:
            if body.scope_id:
                cursor.execute(
                    "SELECT scope FROM chat_tagged_memory WHERE scope_id=%s LIMIT 1", (owner,)
                )
                row = cursor.fetchone()
                if row is None or row["scope"] != body.scope:
                    raise KnowledgeError("MEMORY_SCOPE_NOT_FOUND", status_code=404)
            result = self._write(
                cursor,
                owner,
                body.scope,
                MemoryArguments(
                    action="save",
                    scope=body.scope,
                    key=body.key,
                    content=body.content,
                    tags=body.tags,
                    topic=body.topic,
                    kind=body.kind,
                    valid_from=body.valid_from,
                    valid_until=body.valid_until,
                    observed_at=body.observed_at,
                ),
                source_kind="admin_confirmation",
            )
            return self._admin_get(cursor, result.records[0].id)

    def admin_get(self, id: int) -> MemoryView:
        """管理员读取当前详情，来源和主题均从同一事务快照产生。"""
        with self._transaction() as cursor:
            return self._admin_get(cursor, id)

    def admin_update(self, id: int, body: MemoryUpdate) -> MemoryView:
        """局部编辑不回填未改的陈旧字段，版本变化明确拒绝。"""
        with self._transaction() as cursor:
            owner = self._row(cursor, id)["scope_id"]
        with self._transaction(owner) as cursor:
            row = self._row(cursor, id)
            self._version(row, body.expected_revision)
            if row["status"] in {"merged", "deleted"}:
                _conflict("已合并或遗忘的事实不能直接编辑")
            changes = body.model_dump(exclude_unset=True, exclude={"expected_revision", "reason"})
            if "tags" in changes:
                changes["tags_json"] = _json(normalize_memory_tags(tuple(changes.pop("tags"))))
            for name in ("valid_from", "valid_until"):
                if name in changes:
                    changes[name] = time_value(changes[name])
            if "topic" in changes:
                changes["topic"] = normalized_content(changes["topic"]).casefold()
            if "kind" in changes:
                changes["importance"] = IMPORTANCE[changes["kind"]]
            if not changes or all(row.get(key) == value for key, value in changes.items()):
                return self._admin_get(cursor, id)
            if "valid_until" in changes:
                self._capacity(cursor, owner, excluding=id)
            changes["source_kind"] = "admin_confirmation"
            self._change(cursor, row, changes, operation="revise", reason=body.reason)
            return self._admin_get(cursor, id)

    def admin_revisions(self, id: int, *, limit: int = 20, cursor: str | None = None) -> Page:
        """历史审计允许查看遗忘记录，仍保持有界、筛选绑定的分页。"""
        page_limit(limit)
        filters = {"memory_id": id}
        before = decode_cursor(cursor, filters)
        with self._transaction() as db:
            self._row(db, id, deleted=True)
            db.execute(
                "SELECT * FROM chat_memory_revision WHERE memory_id=%s"
                + (" AND id<%s" if before is not None else "")
                + " ORDER BY id DESC LIMIT %s",
                (id, *((before,) if before else ()), limit + 1),
            )
            rows = db.fetchall()
            items = tuple(
                MemoryRevisionView(
                    id=row["id"],
                    memory_id=id,
                    revision=row["revision"],
                    operation=row["operation"],
                    snapshot=MemoryView.model_validate_json(row["snapshot_json"]),
                    reason=row["reason"],
                    created_at=_public_time(row["created_at"]),
                )
                for row in rows[:limit]
            )
            return Page(
                items=list(items),
                next_cursor=encode_cursor(items[-1].id, filters) if len(rows) > limit else None,
            )

    def admin_list(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        query: str = "",
        tag: str = "",
        scope: str | None = None,
        scope_id: str | None = None,
        topic: str = "",
        status: str | None = None,
        kind: str | None = None,
        subject: str | None = None,
    ) -> Page:
        """列表先筛选有效性再分页，过期记录只在明确状态筛选时显示。"""
        page_limit(limit)
        filters = {
            "query": query,
            "tag": tag,
            "scope": scope or "",
            "scope_id": scope_id or "",
            "topic": topic,
            "status": status or "",
            "kind": kind or "",
            "subject": subject or "",
        }
        before = decode_cursor(cursor, filters)
        conditions, args = [], []
        for column, value in (
            ("scope", scope),
            ("scope_id", scope_id),
            ("topic", topic),
            ("kind", kind),
            ("subject_id", subject),
        ):
            if value:
                conditions.append(f"{column}=%s")
                args.append(value)
        if query:
            conditions.append("(memory_key LIKE %s ESCAPE '!' OR content LIKE %s ESCAPE '!')")
            args.extend((literal_like(query), literal_like(query)))
        if before:
            conditions.append("id<%s")
            args.append(before)
        now = utc_now()
        if status == "all":
            conditions.append("is_deleted=0")
        elif status == "expired":
            conditions.append("(status='expired' OR (status='active' AND valid_until<=%s))")
            args.append(now)
        elif status == "future":
            conditions.append(
                "status='active' AND valid_from>%s AND (valid_until IS NULL OR valid_until>%s)"
            )
            args.extend((now, now))
        elif not status or status == "active":
            conditions.append(_ACTIVE_SQL)
            args.extend((now, now))
        else:
            conditions.append("status=%s")
            args.append(status)
        with self._transaction() as db:
            db.execute(
                "SELECT * FROM chat_tagged_memory"
                + (" WHERE " + " AND ".join(conditions) if conditions else "")
                + " ORDER BY id DESC"
                + (" LIMIT %s" if not tag else ""),
                (*args, *((limit + 1,) if not tag else ())),
            )
            rows = [
                row
                for row in db.fetchall()
                if (
                    effective_status(row) == status
                    if status and status != "all"
                    else not row["is_deleted"]
                    if status == "all"
                    else effective_status(row) == "active"
                )
                and (not tag or tag in json.loads(row["tags_json"]))
            ]
            items = self._admin_views(db, rows[:limit])
            return Page(
                items=list(items),
                next_cursor=encode_cursor(items[-1].id, filters) if len(rows) > limit else None,
            )

    def admin_statistics(self, *, start_time: datetime, end_time: datetime) -> MemoryStatistics:
        """管理端只聚合数量；事实按当前有效性，召回按 UTC 左闭右开区间计数。"""
        now = utc_now()
        with self._transaction() as cursor:
            cursor.execute(
                "SELECT COUNT(*) AS memory_count FROM chat_tagged_memory WHERE " + _ACTIVE_SQL,
                (now, now),
            )
            memory_count = cursor.fetchone()["memory_count"]
            cursor.execute(
                "SELECT COUNT(*) AS recall_count, "
                "COUNT(CASE WHEN JSON_EXTRACT(result_json,'$.hit_count') > 0 "
                "THEN 1 END) AS recall_hit_count FROM chat_memory_operation "
                "WHERE operation_kind='recall_attempt' AND created_at >= %s AND created_at < %s",
                (start_time, end_time),
            )
            row = cursor.fetchone()
        return MemoryStatistics(
            memory_count=memory_count, **row,
            recall_rate=(
                row["recall_hit_count"] / row["recall_count"] if row["recall_count"] else None
            ),
        )

    def admin_tags(
        self, *, scope: str | None = None, scope_id: str | None = None
    ) -> tuple[TagCount, ...]:
        """标签统计不包含过期事实，筛选和列表使用同一有效性。"""
        from collections import Counter

        now = utc_now()
        conditions = [_ACTIVE_SQL]
        args = [now, now]
        for column, value in (("scope", scope), ("scope_id", scope_id)):
            if value:
                conditions.append(f"{column}=%s")
                args.append(value)
        with self._transaction() as db:
            db.execute(
                "SELECT scope,scope_id,tags_json FROM chat_tagged_memory WHERE "
                + " AND ".join(conditions),
                tuple(args),
            )
            counts = Counter(tag for row in db.fetchall() for tag in json.loads(row["tags_json"]))
            return tuple(TagCount(tag=tag, count=count) for tag, count in counts.most_common())

    def admin_scopes(self, *, scope: str | None = None) -> tuple[ScopeView, ...]:
        """墓碑保留作用域目录，计数只统计当前有效事实。"""
        now = utc_now()
        with self._transaction() as db:
            db.execute(
                "SELECT scope_id,scope,SUM(CASE WHEN "
                + _ACTIVE_SQL
                + " THEN 1 ELSE 0 END) AS memory_count FROM chat_tagged_memory"
                + (" WHERE scope=%s" if scope else "")
                + " GROUP BY scope_id,scope ORDER BY scope_id,scope",
                (now, now, *((scope,) if scope else ())),
            )
            return tuple(
                ScopeView(scope_id=row["scope_id"], scope=row["scope"], count=row["memory_count"])
                for row in db.fetchall()
            )

    def admin_delete(self, id: int, *, expected_revision: int | None = None) -> None:
        """遗忘保留全部版本，但普通读取立即停止返回正文。"""
        with self._transaction() as db:
            owner = self._row(db, id)["scope_id"]
        with self._transaction(owner) as db:
            row = self._row(db, id)
            self._version(row, expected_revision)
            self._change(
                db,
                row,
                {"status": "deleted", "is_deleted": 1},
                operation="forget",
                reason="管理员明确遗忘",
            )

    def _batch_rows(self, cursor: Any, items: tuple[MemoryTarget, ...]) -> list[dict[str, Any]]:
        """显式目标必须唯一、同范围、同版本，不能把主题字符串当删除条件。"""
        ids = [item.id for item in items]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate memory target")
        rows = [self._row(cursor, item.id) for item in items]
        if len({row["scope_id"] for row in rows}) != 1:
            _conflict("批量操作必须位于同一作用域")
        for row, item in zip(rows, items, strict=True):
            self._version(row, item.expected_revision)
        return rows

    def admin_merge_preview(self, body: MemoryMerge) -> dict[str, Any]:
        """合并预览展示每个事实，不根据语义相似擅自改数据。"""
        with self._transaction() as db:
            rows = self._batch_rows(db, body.items)
            if body.target_id not in {row["id"] for row in rows}:
                raise ValueError("target must be included")
            if len({(row["subject_type"], row["subject_id"]) for row in rows}) != 1:
                _conflict("不同主体不能合并")
            if any(row["status"] in {"merged", "deleted"} for row in rows):
                _conflict("已合并或遗忘记录不能参与合并")
            items = self._admin_views(db, rows)
            return {
                "items": items,
                "target_id": body.target_id,
                "action": "merge",
                "digest": preview_digest(list(items), "merge", body.target_id),
            }

    def admin_merge(self, body: MemoryMerge) -> MemoryView:
        """一次提交目标修订和所有源墓碑；素材引用不扩大原件权限。"""
        preview = self.admin_merge_preview(body)
        owner = preview["items"][0].scope_id
        with self._transaction(owner) as db:
            rows = self._batch_rows(db, body.items)
            items = self._admin_views(db, rows)
            if body.digest != preview_digest(list(items), "merge", body.target_id):
                _conflict("合并预览已变化，请重新核对")
            target = next(row for row in rows if row["id"] == body.target_id)
            tags = normalize_memory_tags(
                tuple(dict.fromkeys(tag for row in rows for tag in json.loads(row["tags_json"])))
            )
            if len(tags) > 8:
                raise ValueError("合并标签超过八个，请先整理标签")
            for row in rows:
                if row["id"] == body.target_id:
                    continue
                self._change(
                    db,
                    row,
                    {"status": "merged", "merged_into_id": body.target_id},
                    operation="merge",
                    reason=body.reason,
                )
                db.execute("SELECT * FROM chat_memory_evidence WHERE memory_id=%s", (row["id"],))
                for source in db.fetchall():
                    db.execute(
                        "SELECT id FROM chat_memory_evidence WHERE memory_id=%s AND "
                        "evidence_fingerprint=%s",
                        (target["id"], source["evidence_fingerprint"]),
                    )
                    if db.fetchone():
                        continue
                    names = (
                        "source_kind",
                        "source_ref",
                        "source_actor_id",
                        "source_session_id",
                        "source_event_id",
                        "source_time",
                        "recorded_at",
                        "excerpt",
                        "evidence_fingerprint",
                    )
                    db.execute(
                        "INSERT INTO chat_memory_evidence (memory_id,first_revision,"
                        + ",".join(names)
                        + ",original_memory_id) VALUES ("
                        + ",".join("%s" for _ in range(len(names) + 3))
                        + ")",
                        (
                            target["id"],
                            target["revision"] + 1,
                            *(source[name] for name in names),
                            source["original_memory_id"] or row["id"],
                        ),
                    )
            self._change(
                db,
                target,
                {"content": body.content or target["content"], "tags_json": _json(tags)},
                operation="merge",
                reason=body.reason,
            )
            return self._admin_get(db, target["id"])

    def admin_cleanup_preview(self, body: MemoryCleanupPreview) -> dict[str, Any]:
        """主题清理上限一百条，必须人工检查明确目标。"""
        with self._transaction() as db:
            db.execute(
                "SELECT * FROM chat_tagged_memory WHERE scope_id=%s AND topic=%s "
                "AND is_deleted=0 AND status<>'merged' ORDER BY id LIMIT 101",
                (body.scope_id, body.topic),
            )
            rows = db.fetchall()
            if len(rows) > 100:
                raise ValueError("主题超过一百条，请按事实选择分批清理")
            items = self._admin_views(db, rows)
            return {
                "items": items,
                "action": body.action,
                "digest": preview_digest(list(items), body.action),
            }

    def admin_cleanup(self, body: MemoryCleanup) -> dict[str, int]:
        """核对预览版本后原子标记失效或遗忘，不触及新出现的事实。"""
        with self._transaction() as db:
            rows = self._batch_rows(db, body.items)
            owner = rows[0]["scope_id"]
        with self._transaction(owner) as db:
            rows = self._batch_rows(db, body.items)
            items = self._admin_views(db, rows)
            if body.digest != preview_digest(list(items), body.action):
                _conflict("清理预览已变化，请重新核对")
            for row in rows:
                changes = (
                    {"status": "deleted", "is_deleted": 1}
                    if body.action == "forget"
                    else _expiration_changes(row)
                )
                self._change(db, row, changes, operation=body.action, reason=body.reason)
            return {"affected": len(rows)}

    def _conflict_rows(self, cursor: Any, id: int) -> list[dict[str, Any]]:
        """冲突集合包含服务端分支和原事实，不能用相似主题冒充同命题。"""
        row = self._row(cursor, id)
        group = row["conflict_set_id"]
        if group is None:
            cursor.execute(
                "SELECT conflict_set_id FROM chat_tagged_memory WHERE scope_id=%s "
                "AND memory_key LIKE %s AND conflict_set_id IS NOT NULL "
                "AND status IN ('pending','disputed') ORDER BY id LIMIT 1",
                (row["scope_id"], f"conflict-{id}-%"),
            )
            branch = cursor.fetchone()
            group = branch["conflict_set_id"] if branch else None
        if group is None:
            raise KnowledgeError("MEMORY_CONFLICT_NOT_FOUND", status_code=404)
        cursor.execute(
            "SELECT * FROM chat_tagged_memory WHERE scope_id=%s AND "
            "conflict_set_id=%s AND is_deleted=0 ORDER BY id LIMIT 101",
            (row["scope_id"], group),
        )
        rows = cursor.fetchall()
        if len(rows) > 100:
            raise ValueError("冲突集合超过本次处理上限")
        parents = {
            int(match.group(1))
            for item in rows
            if (match := re.fullmatch(r"conflict-(\d+)-[a-f0-9]{16}", item["memory_key"]))
        }
        existing = {item["id"] for item in rows}
        for parent in parents - existing:
            original = self._row(cursor, parent)
            if original["scope_id"] != row["scope_id"]:
                _conflict("冲突来源不属于同一作用域")
            rows.append(original)
        if id not in {item["id"] for item in rows}:
            _conflict("该事实不属于冲突集合")
        return rows

    def admin_conflicts(self, id: int) -> Page:
        """管理员可查看同命题所有候选及版本，再明确采用其中一项。"""
        with self._transaction() as cursor:
            return Page(
                items=list(self._admin_views(cursor, self._conflict_rows(cursor, id))),
                next_cursor=None,
            )

    def admin_resolve(self, body: MemoryResolve) -> MemoryView:
        """所有受影响版本一次仲裁，旧结论保留历史并退出默认召回。"""
        with self._transaction() as cursor:
            owner = self._row(cursor, body.target_id)["scope_id"]
        with self._transaction(owner) as cursor:
            rows = self._batch_rows(cursor, body.items)
            entire = self._conflict_rows(cursor, body.target_id)
            if {row["id"] for row in rows} != {row["id"] for row in entire}:
                _conflict("必须核对整个冲突集合，不能遗漏候选版本")
            if len({(row["subject_id"], row["subject_type"]) for row in rows}) != 1:
                _conflict("不同主体的冲突不能一起解决")
            target = next(row for row in rows if row["id"] == body.target_id)
            self._capacity(cursor, owner, excluding=target["id"])
            for row in rows:
                if row["id"] == target["id"]:
                    changes = {
                        "status": "active",
                        "conflict_set_id": None,
                        "source_kind": "admin_confirmation",
                    }
                else:
                    changes = {"status": "expired", "conflict_set_id": None}
                self._change(cursor, row, changes, operation="resolve", reason=body.reason)
            return self._admin_get(cursor, target["id"])
