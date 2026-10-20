"""使用隔离 SQLite 核验事实修订、去重、时效和版本竞争。"""

from datetime import datetime, timedelta, timezone

import pytest
from test_tagged_memory import memory_store as _memory_store

from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity

memory_store = _memory_store


def test_exact_duplicate_reuses_fact_across_keys(memory_store):
    """不同键的相同命题不能继续制造重复正文。"""
    identity = MemoryIdentity(bot_id="100", user_id="101", session_id="session")
    first = memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="gateway",
            content="网关请求体上限为 8 MB",
            tags=["网关"],
        ),
    )
    second = memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="gateway-copy",
            content="网关请求体上限为 8 MB",
            tags=["413"],
        ),
    )
    assert first.status == "saved"
    assert second.status == "deduplicated"
    assert second.records[0].id == first.records[0].id
    assert second.records[0].revision == 1


def test_revision_preserves_history_and_rejects_stale_writer(memory_store):
    """明确修订只推进当前版本，陈旧写入不能覆盖新结论。"""
    from agent_bot.memory.contracts import MemoryUpdate

    identity = MemoryIdentity(bot_id="100", user_id="101", session_id="session")
    first = memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="limit",
            content="上限是 8 MB",
            tags=["网关"],
        ),
    ).records[0]
    updated = memory_store.admin_update(
        first.id,
        MemoryUpdate(
            expected_revision=1,
            content="上限已明确改为 16 MB",
            reason="管理员核对配置",
        ),
    )
    assert updated.revision == 2
    assert updated.tags == ("网关",)
    history = memory_store.admin_revisions(first.id)
    assert {item.revision for item in history.items} == {1, 2}
    assert (
        next(item for item in history.items if item.revision == 1).snapshot.content == "上限是 8 MB"
    )
    with pytest.raises(RuntimeError, match="版本"):
        memory_store.admin_update(
            first.id,
            MemoryUpdate(
                expected_revision=1,
                content="陈旧内容",
                reason="重复提交",
            ),
        )


def test_expiration_and_tombstone_do_not_reappear(memory_store):
    """时间到期停止召回，遗忘正文不能通过换键原样恢复。"""
    identity = MemoryIdentity(bot_id="100", user_id="101", session_id="session")
    record = memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="temporary",
            content="临时活动安排",
            tags=["活动"],
            valid_until=datetime.now(timezone.utc) - timedelta(seconds=1),
        ),
    ).records[0]
    assert (
        memory_store.execute(identity, MemoryArguments(action="recall", tags=["活动"])).records
        == ()
    )
    memory_store.admin_delete(record.id, expected_revision=1)
    with pytest.raises(RuntimeError, match="遗忘"):
        memory_store.execute(
            identity,
            MemoryArguments(
                action="save",
                key="temporary-copy",
                content="临时活动安排",
                tags=["活动"],
            ),
        )


@pytest.mark.parametrize("kind", ["fact", "agreement"])
def test_forgotten_fact_cannot_be_restored_by_changing_kind(memory_store, kind):
    """分类属于模型推断，改分类不能绕过同主体正文的遗忘墓碑。"""
    identity = MemoryIdentity(bot_id="1", user_id="2", session_id="s")
    saved = memory_store.execute(
        identity,
        MemoryArguments(action="save", key="old", content="周五见面", tags=["约定"]),
    ).records[0]
    memory_store.admin_delete(saved.id, expected_revision=1)
    with pytest.raises(RuntimeError, match="遗忘"):
        memory_store.execute(
            identity,
            MemoryArguments(action="save", key="new", content="周五见面", tags=["约定"], kind=kind),
        )


@pytest.mark.parametrize("key", ["old", "new"])
def test_save_cannot_return_a_merged_source(memory_store, key):
    """合并源已退出当前事实集合，重存不能返回已被新结论替代的正文。"""
    from agent_bot.memory.contracts import MemoryMerge, MemoryTarget

    identity = MemoryIdentity(bot_id="1", user_id="2", session_id="s")
    sources = [
        memory_store.execute(
            identity, MemoryArguments(action="save", key=name, content=text, tags=["网关"])
        ).records[0]
        for name, text in (("target", "当前配置"), ("old", "过去配置"))
    ]
    body = MemoryMerge(
        target_id=sources[0].id,
        items=tuple(MemoryTarget(id=item.id, expected_revision=1) for item in sources),
        content="已确认的完整配置",
        reason="按主题核对并合并",
    )
    preview = memory_store.admin_merge_preview(body)
    memory_store.admin_merge(body.model_copy(update={"digest": preview["digest"]}))
    with pytest.raises(RuntimeError, match="合并"):
        memory_store.execute(
            identity,
            MemoryArguments(action="save", key=key, content="过去配置", tags=["网关"]),
        )


@pytest.mark.parametrize("key", ["active", "new"])
def test_existing_active_duplicate_does_not_hide_a_tombstone(memory_store, key):
    """即使先命中同键或同分类活事实，也必须检查其他分类留下的原文墓碑。"""
    identity = MemoryIdentity(bot_id="1", user_id="2", session_id="s")
    for name, kind in (("active", "fact"), ("forgotten", "agreement")):
        saved = memory_store.execute(
            identity,
            MemoryArguments(action="save", key=name, kind=kind, content="同一事实", tags=["主题"]),
        ).records[0]
    memory_store.admin_delete(saved.id, expected_revision=1)
    with pytest.raises(RuntimeError, match="遗忘"):
        memory_store.execute(
            identity,
            MemoryArguments(action="save", key=key, content="同一事实", tags=["主题"]),
        )


def test_partial_update_rejects_explicit_null_content():
    """null 只能清空允许为空的时间，不能清空事实正文。"""
    from pydantic import ValidationError

    from agent_bot.memory.contracts import MemoryUpdate

    with pytest.raises(ValidationError):
        MemoryUpdate(expected_revision=1, content=None, reason="错误清空")


def test_reactivating_expired_fact_respects_current_capacity(memory_store):
    """延长到期事实的有效期也占当前名额，不能通过修订突破容量。"""
    identity = MemoryIdentity(bot_id="1", user_id="2", session_id="s", source_text="更新约定")
    saved = memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            scope="user",
            key="old",
            content="更新约定",
            tags=["约定"],
            source_excerpt="更新约定",
            valid_until=datetime.now(timezone.utc) - timedelta(days=1),
        ),
    ).records[0]
    owner = memory_store.admin_get(saved.id).scope_id
    with memory_store._transaction(owner) as db:
        for index in range(256):
            db.execute(
                "INSERT INTO chat_tagged_memory "
                "(scope_id,memory_key,content,scope,subject_type,subject_id,create_time,update_time,"
                "tags_json,material_ids_json) VALUES (%s,%s,%s,'user','user',%s,%s,%s,'[]','[]')",
                (
                    owner,
                    f"active-{index}",
                    f"其他事实{index}",
                    owner,
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc),
                ),
            )
    with pytest.raises(RuntimeError, match="已满"):
        memory_store.execute(
            identity,
            MemoryArguments(
                action="revise",
                scope="user",
                id=saved.id,
                expected_revision=1,
                content="更新约定",
                source_excerpt="更新约定",
                retention_reason="correction",
                reason="延长约定",
                valid_until=datetime.now(timezone.utc) + timedelta(days=1),
            ),
        )


def test_candidate_read_does_not_load_expired_history(memory_store, monkeypatch):
    """保留的过期历史不应进入模型召回事务的数据集，避免读取成本线性增长。"""
    from contextlib import contextmanager

    identity = MemoryIdentity(bot_id="1", user_id="2", session_id="s")
    saved = memory_store.execute(
        identity,
        MemoryArguments(action="save", key="current", content="当前事实", tags=["主题"]),
    ).records[0]
    owner = memory_store.admin_get(saved.id).scope_id
    with memory_store._transaction(owner) as db:
        for index in range(32):
            db.execute(
                "INSERT INTO chat_tagged_memory "
                "(scope_id,memory_key,content,scope,status,create_time,update_time,"
                "tags_json,material_ids_json) "
                "VALUES (%s,%s,%s,'session','expired',%s,%s,'[]','[]')",
                (
                    owner,
                    f"expired-{index}",
                    f"过期事实{index}",
                    datetime.now(timezone.utc),
                    datetime.now(timezone.utc),
                ),
            )
    transaction = memory_store._transaction
    loaded = []

    class ObservedCursor:
        """在数据库边界记录实际读取行数，而非只核对过滤后的返回值。"""

        def __getattr__(self, name):
            """委托真实隔离数据库保持原执行语义。"""
            return getattr(self.cursor, name)

        def execute(self, sql, args=()):
            """仅统计当前事实候选查询，证据等附属读取另有独立上限。"""
            self.candidate_query = sql.startswith(
                "SELECT * FROM chat_tagged_memory WHERE scope_id IN"
            )
            return self.cursor.execute(sql, args)

        def fetchall(self):
            """记录存储层实际加载的结果规模。"""
            rows = self.cursor.fetchall()
            if self.candidate_query:
                loaded.append(len(rows))
            return rows

    @contextmanager
    def observed(*args, **kwargs):
        """仅包装本次召回连接，不连接外部数据库。"""
        with transaction(*args, **kwargs) as db:
            cursor = ObservedCursor()
            cursor.cursor = db
            yield cursor

    monkeypatch.setattr(memory_store, "_transaction", observed)
    rows = memory_store.read_candidates(identity, MemoryArguments(action="recall", query="当前"))
    assert [row["id"] for row in rows] == [saved.id]
    memory_store.execute(identity, MemoryArguments(action="recall", tags=["主题"]))
    memory_store.execute(identity, MemoryArguments(action="tags"))
    assert loaded == [1, 1, 1]


def test_new_independent_confirmation_creates_auditable_version(memory_store):
    """同一命题的新真实来源应追加证据，而非永远忽略后续确认。"""
    first = MemoryIdentity(
        bot_id="1", user_id="2", session_id="s", source_text="我喜欢清淡", source_event_id="event-1"
    )
    second = MemoryIdentity(
        bot_id="1", user_id="2", session_id="s", source_text="我喜欢清淡", source_event_id="event-2"
    )
    arguments = MemoryArguments(
        action="save",
        scope="user",
        key="taste",
        content="喜欢清淡",
        tags=["口味"],
        source_excerpt="我喜欢清淡",
        retention_reason="stable_preference",
        kind="preference",
    )
    memory_store.execute(first, arguments)
    result = memory_store.execute(second, arguments)
    assert result.status == "confirmed"
    assert result.records[0].revision == 2
    assert len(result.records[0].sources) == 2
    replay = memory_store.execute(second, arguments)
    assert replay.status == "confirmed"  # 重放返回原成功回执，不再次推进版本。
    assert replay.records[0].revision == 2


def test_conflicting_shared_fact_requires_review_and_retains_both_sources(memory_store):
    """同等级的共享结论冲突不能按最近一条直接覆盖。"""
    first = MemoryIdentity(
        bot_id="1",
        user_id="2",
        session_id="s",
        group_id="3",
        source_text="网关上限8MB",
        source_event_id="event-1",
    )
    second = MemoryIdentity(
        bot_id="1",
        user_id="4",
        session_id="s",
        group_id="3",
        source_text="网关上限16MB",
        source_event_id="event-2",
    )
    saved = memory_store.execute(
        first,
        MemoryArguments(
            action="save",
            scope="group",
            key="limit",
            content="网关上限8MB",
            tags=["网关"],
            source_excerpt="网关上限8MB",
            retention_reason="reusable_fact",
        ),
    ).records[0]
    result = memory_store.execute(
        second,
        MemoryArguments(
            action="revise",
            scope="group",
            id=saved.id,
            expected_revision=1,
            content="网关上限16MB",
            source_excerpt="网关上限16MB",
            retention_reason="correction",
            reason="新报告与原结论不同",
        ),
    )
    assert result.status == "disputed"
    assert memory_store.admin_get(saved.id).status == "disputed"
    assert (
        memory_store.execute(
            first, MemoryArguments(action="recall", scope="group", tags=["网关"])
        ).records
        == ()
    )


def test_patch_cannot_duplicate_another_fact(memory_store):
    """编辑不能制造第二份同主体正文，管理员应明确使用合并。"""
    from agent_bot.memory.contracts import MemoryUpdate

    identity = MemoryIdentity(bot_id="1", user_id="2", session_id="s")
    a = memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="a",
            content="第一条",
            tags=["主题"],
        ),
    ).records[0]
    memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="b",
            content="第二条",
            tags=["主题"],
        ),
    )
    with pytest.raises(RuntimeError, match="合并"):
        memory_store.admin_update(
            a.id,
            MemoryUpdate(
                expected_revision=1,
                content="第二条",
                reason="试图重复",
            ),
        )


def test_adoption_requires_actual_recall_and_is_idempotent(memory_store):
    """仅投递中实际引用且本轮召回过的事实计热度，重放不重复计数。"""
    identity = MemoryIdentity(bot_id="1", user_id="2", session_id="s", request_id="req-1")
    saved = memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="a",
            content="第一条",
            tags=["主题"],
        ),
    ).records[0]
    citation = f"答复【记忆 #{saved.id} v1】"
    memory_store.record_adoption("s", "req-1", citation)
    assert memory_store.admin_get(saved.id).last_used_at is None
    memory_store.execute(identity, MemoryArguments(action="recall", tags=["主题"]))
    assert memory_store.admin_get(saved.id).last_used_at is None
    memory_store.record_adoption("s", "req-1", citation)
    memory_store.record_adoption("s", "req-1", citation)
    with memory_store._transaction() as db:
        row = memory_store._row(db, saved.id)
    assert row["use_count"] == 1
    assert row["last_used_at"] is not None


def test_same_operation_replay_does_not_overwrite_a_new_revision(memory_store):
    """已提交操作重放只返回回执，不能覆盖之后的新版本。"""
    identity = MemoryIdentity(
        bot_id="1", user_id="2", session_id="s", source_text="我喜欢清淡", source_event_id="event-1"
    )
    arguments = MemoryArguments(
        action="save",
        scope="user",
        key="taste",
        content="喜欢清淡",
        tags=["口味"],
        source_excerpt="我喜欢清淡",
        retention_reason="stable_preference",
        operation_id="op-1",
    )
    first = memory_store.execute(identity, arguments)
    replay = memory_store.execute(identity, arguments)
    assert first.status == replay.status == "saved"
    assert replay.records[0].revision == 1
    changed = arguments.model_copy(update={"content": "另一事实"})
    with pytest.raises(RuntimeError, match="操作"):
        memory_store.execute(identity, changed)


def test_administrator_resolves_entire_conflict_set(memory_store):
    """选定一个已存在结论后，其他冲突版本必须一起失效。"""
    from agent_bot.memory.contracts import MemoryResolve, MemoryTarget

    first = MemoryIdentity(
        bot_id="1",
        user_id="2",
        session_id="s",
        group_id="3",
        source_text="网关上限8MB",
        source_event_id="event-1",
    )
    second = MemoryIdentity(
        bot_id="1",
        user_id="4",
        session_id="s",
        group_id="3",
        source_text="网关上限16MB",
        source_event_id="event-2",
    )
    saved = memory_store.execute(
        first,
        MemoryArguments(
            action="save",
            scope="group",
            key="limit",
            content="网关上限8MB",
            tags=["网关"],
            source_excerpt="网关上限8MB",
            retention_reason="reusable_fact",
        ),
    ).records[0]
    proposed = memory_store.execute(
        second,
        MemoryArguments(
            action="revise",
            scope="group",
            id=saved.id,
            expected_revision=1,
            content="网关上限16MB",
            source_excerpt="网关上限16MB",
            retention_reason="correction",
            reason="新报告",
        ),
    ).records[0]
    conflicts = memory_store.admin_conflicts(proposed.id).items
    adopted = memory_store.admin_resolve(
        MemoryResolve(
            target_id=proposed.id,
            items=tuple(
                MemoryTarget(id=item.id, expected_revision=item.revision) for item in conflicts
            ),
            reason="管理员查配置确认16MB",
        )
    )
    assert adopted.status == "active"
    recalled = memory_store.execute(
        first, MemoryArguments(action="recall", scope="group", tags=["网关"])
    )
    assert [item.content for item in recalled.records] == ["网关上限16MB"]


def test_source_time_does_not_become_fact_time(memory_store):
    """今天提及的往事不能自动标为今天发生。"""
    identity = MemoryIdentity(
        bot_id="1",
        user_id="2",
        session_id="s",
        source_text="以前发生过413",
        source_event_id="event-1",
        source_time=datetime.now(timezone.utc),
    )
    saved = memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="incident",
            content="以前发生过413",
            tags=["网关"],
            source_excerpt="以前发生过413",
            retention_reason="reusable_fact",
        ),
    ).records[0]
    assert saved.observed_at is None
    assert saved.sources[0].source_time is not None


def test_task_completion_changes_state_and_does_not_remain_open(memory_store):
    """已完成任务保留完成事实，但不继续作为高优先级待办。"""
    from agent_bot.memory.contracts import MemoryUpdate

    identity = MemoryIdentity(bot_id="1", user_id="2", session_id="s")
    saved = memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="task",
            content="核对网关配置",
            tags=["待办"],
            kind="task",
        ),
    ).records[0]
    completed = memory_store.admin_update(
        saved.id,
        MemoryUpdate(
            expected_revision=1,
            content="已核对网关配置",
            task_state="completed",
            reason="用户确认完成",
        ),
    )
    assert completed.task_state == "completed"
    assert completed.importance == 0.5


def test_shortening_quote_does_not_create_an_independent_source(memory_store):
    """同一消息换截取片段不能冒充第二个独立来源。"""
    identity = MemoryIdentity(
        bot_id="1",
        user_id="2",
        session_id="s",
        source_text="今天我喜欢清淡",
        source_event_id="same-event",
    )
    arguments = MemoryArguments(
        action="save",
        scope="user",
        key="taste",
        content="喜欢清淡",
        tags=["口味"],
        source_excerpt="今天我喜欢清淡",
        retention_reason="stable_preference",
    )
    memory_store.execute(identity, arguments)
    result = memory_store.execute(
        identity, arguments.model_copy(update={"source_excerpt": "喜欢清淡"})
    )
    assert result.status == "unchanged"
    assert result.records[0].revision == 1
    assert len(result.records[0].sources) == 1


def test_expiring_future_fact_keeps_a_valid_time_interval(memory_store):
    """提前撤销未来生效的约定不能生成结束早于开始的区间。"""
    from agent_bot.memory.contracts import MemoryCleanup, MemoryCleanupPreview, MemoryTarget

    identity = MemoryIdentity(bot_id="1", user_id="2", session_id="s")
    start = datetime.now(timezone.utc) + timedelta(days=1)
    saved = memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="future",
            content="明天的约定",
            tags=["约定"],
            valid_from=start,
            valid_until=start + timedelta(days=1),
        ),
    ).records[0]
    item = memory_store.admin_get(saved.id)
    preview = memory_store.admin_cleanup_preview(
        MemoryCleanupPreview(
            scope_id=item.scope_id,
            topic=item.topic,
            action="expire",
        )
    )
    memory_store.admin_cleanup(
        MemoryCleanup(
            items=[MemoryTarget(id=saved.id, expected_revision=1)],
            action="expire",
            digest=preview["digest"],
            reason="提前撤销约定",
        )
    )
    expired = memory_store.admin_get(saved.id)
    assert expired.effective_status == "expired"
    assert expired.valid_until > expired.valid_from
