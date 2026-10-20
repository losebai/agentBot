"""统一上下文使用独立 SQLite 真事务验证并发、代际和平台确认边界。"""

import importlib.util
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from agent_bot.settings import DatabaseParams


@pytest.fixture(scope="session", autouse=True)
def after_nonebot_init():
    """纯数据库仓库不启动 QQ 插件，避免接入层装配影响隔离事务测试。"""
    return None


def test_unified_session_context_is_available():
    """新仓库必须作为独立运行模块提供，而非继续拼接旧历史表。"""
    assert importlib.util.find_spec("agent_bot.session_context") is not None


@pytest.fixture
def context_store(tmp_path):
    """只创建本测试所需新表，不读取部署配置、数据库或生产凭据。"""
    from agent_bot.session_context import SessionContextPolicy, SessionContextStore

    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "context.sqlite3"))
    with sqlite3.connect(params.sqlite_path) as connection:
        connection.execute("""
            CREATE TABLE chat_session_context (
                session_id TEXT PRIMARY KEY, generation_id TEXT NOT NULL,
                messages_json TEXT NOT NULL, memory_summary TEXT NOT NULL,
                memory_facts_json TEXT,
                legacy_memory_summary TEXT NOT NULL DEFAULT '',
                memory_revision INTEGER NOT NULL, deliveries_json TEXT NOT NULL,
                create_time TEXT NOT NULL, update_time TEXT NOT NULL
            )
        """)
    now = [datetime(2026, 10, 19, tzinfo=UTC)]
    policy = SessionContextPolicy(50, 80_000, False, 4000)
    store = SessionContextStore(params, policy=policy, clock=lambda: now[0])
    return store, params, policy, now


def contents(store, session="s", **kwargs):
    """提取有序模型消息，便于只断言行为而不依赖数据库 JSON 内部结构。"""
    return [(message.role, message.content)
            for message in store.context(session, **kwargs).messages]


def test_received_messages_survive_restart_without_ttl_and_exclude_only_current_user(context_store):
    """接收即持久化，时间推进不删除；当前输入只排除自身用户事件。"""
    from agent_bot.session_context import SessionContextStore

    store, params, policy, now = context_store
    assert store.context("s").generation_id is None
    generation = store.ensure_generation("s")
    store.record_received("s", "当前输入", request_id="r1", generation_id=generation)
    store.record("s", generation, "a" * 64, "r1", "qq-1", "已发送回应", ())
    now[0] += timedelta(days=730)
    restored = SessionContextStore(params, policy=policy, clock=lambda: now[0])
    assert restored.ensure_generation("s") == generation
    assert contents(restored) == [("user", "当前输入"), ("assistant", "已发送回应")]
    assert contents(restored, exclude_request_id="r1") == [("assistant", "已发送回应")]
    assert contents(restored, "other") == []
    with sqlite3.connect(params.sqlite_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM chat_session_context").fetchone()[0] == 1


def test_received_events_merge_by_utc_time_and_keep_same_time_lock_order(context_store):
    """迟到事件插入时间位置，同一时间则保持事务领取的稳定顺序。"""
    store, _, _, now = context_store
    at = now[0]
    store.record_received("s", "第三条", request_id="r3", created_at=at + timedelta(seconds=2))
    store.record_received("s", "第一条", request_id="r1", created_at=at)
    store.record_received("s", "第二条", request_id="r2", created_at=at + timedelta(seconds=1))
    store.record_received("s", "同秒后到", request_id="r4", created_at=at)
    assert [text for _, text in contents(store)] == ["第一条", "同秒后到", "第二条", "第三条"]
    store.record_received("s", "第一条", request_id="r1", created_at=at)
    assert len(contents(store)) == 4
    with pytest.raises(ValueError, match="identity conflicts"):
        store.record_received("s", "篡改第一条", request_id="r1", created_at=at)


def test_parallel_connections_merge_without_message_version_conflicts(context_store):
    """多个实例同时首次创建并更新同一行，数据库事务不得丢写或用版本拒绝消息。"""
    from agent_bot.session_context import SessionContextStore

    store, params, policy, now = context_store
    barrier = Barrier(8)

    def append(index):
        """每个线程持独立仓库和连接，避免进程内单实例锁掩盖事务问题。"""
        local = SessionContextStore(params, policy=policy, clock=lambda: now[0])
        barrier.wait(timeout=5)
        local.record_received("s", f"并发输入{index}", request_id=f"r{index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(append, range(8)))
    assert {text for _, text in contents(store)} == {f"并发输入{index}" for index in range(8)}
    assert len(contents(store)) == 8


def test_commit_uses_registered_input_and_actual_delivery_instead_of_fake_full_reply(context_store):
    """轮次提交不重复接收输入，不追加未真实发送的模型整段或任务状态摘要。"""
    store, _, _, _ = context_store
    generation = store.ensure_generation("s")
    store.record_received("s", "用户原文", request_id="r1")
    store.record("s", generation, "a" * 64, "r1", "qq-1", "实际发送的第一段", ())
    store.commit_exchange("s", "用户原文", "未真正发送的全部模型内容", request_id="r1")
    store.commit_exchange("s", "用户原文", "未真正发送的全部模型内容", request_id="r1")
    assert contents(store) == [("user", "用户原文"), ("assistant", "实际发送的第一段")]
    store.commit_exchange("s", "兼容输入", "兼容回答", request_id="r2")
    assert contents(store)[-2:] == [("user", "兼容输入"), ("assistant", "兼容回答")]


def test_pending_delivery_is_invisible_and_finish_is_idempotent(context_store):
    """发送占位不等于成功；重复占位阻止重发，确认后只添加一次助手事件。"""
    from agent_bot.sent_messages import SentMessageUncertainError

    store, _, _, now = context_store
    generation = store.ensure_generation("s")
    values = ("s", generation, "a" * 64, "r1", None, "文件原文\n\n内容", ("material-1",))
    assert store.claim_delivery(*values) is None
    assert store.pending_deliveries("s", generation) == ("a" * 64,)
    assert contents(store) == [] and store.list_messages("s", generation) == ()
    with pytest.raises(SentMessageUncertainError):
        store.claim_delivery(*values)
    now[0] += timedelta(seconds=1)
    first = store.finish_delivery("s", "a" * 64, "qq-1")
    now[0] += timedelta(seconds=1)
    assert store.finish_delivery("s", "a" * 64, "qq-1") == first
    assert store.claim_delivery(*values) == first
    assert first.material_ids == ("material-1",)
    assert contents(store) == [("assistant", "文件原文\n\n内容")]
    assert store.pending_deliveries("s", generation) == ()
    with pytest.raises(ValueError, match="receipt conflicts"):
        store.finish_delivery("s", "a" * 64, "another-id")


def test_clear_changes_generation_keeps_receipts_and_rejects_late_old_requests(context_store):
    """清空保留发送幂等身份；旧输入提交和旧发送确认均不能进入新代际。"""
    store, _, _, _ = context_store
    old = store.ensure_generation("s")
    store.record_received("s", "旧输入", request_id="r1", generation_id=old)
    values = ("s", old, "a" * 64, "r1", None, "旧发送", ())
    store.claim_delivery(*values)
    assert store.clear("s")
    current = store.ensure_generation("s")
    assert current != old
    store.commit_exchange("s", "旧输入", "迟到提交", request_id="r1")
    store.finish_delivery("s", "a" * 64, "qq-old")
    assert contents(store) == []
    assert store.list_messages("s", old) == store.list_messages("s", current) == ()
    assert store.claim_delivery(*values).message_id == "qq-old"
    assert not store.record("s", old, "b" * 64, "r1", "qq-new", "另一旧发送", ())
    store.record_received("s", "新输入", request_id="r2", generation_id=current)
    assert contents(store) == [("user", "新输入")]


@pytest.mark.parametrize("change", [
    {"generation_id": "b" * 32}, {"request_id": "other"},
    {"content": "篡改"}, {"material_ids": ("foreign",)},
])
def test_completed_delivery_identity_is_immutable_even_after_clear(context_store, change):
    """清空不能让同一投递回执代表另一份内容、请求、素材或代际。"""
    store, _, _, _ = context_store
    values = dict(session_id="s", generation_id=store.ensure_generation("s"),
                  delivery_id="a" * 64, request_id="r1", message_id=None,
                  content="正文", material_ids=("material-1",))
    store.claim_delivery(**values)
    store.finish_delivery("s", "a" * 64, "qq-1")
    store.clear("s")
    with pytest.raises(ValueError, match="identity conflicts"):
        store.claim_delivery(**(values | change))


def test_concurrent_claims_allow_one_sender_only(context_store):
    """同一幂等投递并发占位时，只有一个事务取得发送授权。"""
    from agent_bot.sent_messages import SentMessageUncertainError
    from agent_bot.session_context import SessionContextStore

    store, params, policy, now = context_store
    generation = store.ensure_generation("s")
    barrier = Barrier(2)

    def claim(_):
        """独立连接争用相同会话行，未知状态只能由显式平台回执恢复。"""
        local = SessionContextStore(params, policy=policy, clock=lambda: now[0])
        barrier.wait(timeout=5)
        try:
            return local.claim_delivery("s", generation, "a" * 64, None, None, "通知", ())
        except SentMessageUncertainError as error:
            return error

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, range(2)))
    assert sum(result is None for result in results) == 1
    assert sum(isinstance(result, SentMessageUncertainError) for result in results) == 1


def test_model_preview_bounds_long_file_without_changing_saved_content(context_store):
    """模型副本保留素材读取提示，当前消息全文留在统一消息数组而非回执正文。"""
    store, params, _, _ = context_store
    generation = store.ensure_generation("s")
    text = "完整代码\n\n" * 15_000
    store.record("s", generation, "a" * 64, None, "qq-1", text, ("original-file",))
    message = store.context("s").messages[0]
    assert len(message.content) <= 32_000
    assert "original-file" in message.content and "material_get" in message.content
    assert store.list_messages("s", generation)[0].content == text
    with sqlite3.connect(params.sqlite_path) as connection:
        row = connection.execute(
            "SELECT messages_json,deliveries_json FROM chat_session_context",
        ).fetchone()
    assert text in [event["content"] for event in json.loads(row[0])]
    assert text not in row[1]


def test_memory_compression_accepts_new_messages_but_rejects_stale_snapshots(context_store):
    """压缩快照限制只作用于摘要替换，并发普通消息始终可以追加。"""
    from agent_bot.session_context import SessionContextPolicy

    store, _, _, _ = context_store
    store.update_policy(SessionContextPolicy(1, 1000, True, 4000))
    store.commit_exchange("s", "输入1", "回答1", request_id="r1")
    compression = store.commit_exchange("s", "输入2", "回答2", request_id="r2")
    assert compression is not None
    assert store.context("s").memory
    store.record_received("s", "并发新输入", request_id="r3")
    assert store.apply_memory(compression, "模型压缩摘要")
    assert "模型压缩摘要" in store.context("s").memory
    assert ("user", "并发新输入") in contents(store)
    next_compression = store.commit_exchange("s", "输入4", "回答4", request_id="r4")
    assert next_compression is not None
    assert not store.apply_memory(compression, "过时摘要")
    store.clear("s")
    assert not store.apply_memory(next_compression, "旧代际摘要")


def test_policy_change_compacts_without_ttl_or_deleting_session(context_store):
    """策略变化仅收紧消息窗口，零历史仍保留稳定代际与不可重发的回执。"""
    from agent_bot.session_context import SessionContextPolicy

    store, _, _, _ = context_store
    generation = store.ensure_generation("s")
    store.commit_exchange("s", "输入", "回答", request_id="r1")
    store.update_policy(SessionContextPolicy(0, 1000, False, 4000))
    store.compact("s")
    assert contents(store) == []
    assert store.ensure_generation("s") == generation
    assert store.history_turns == 0 and store.max_history_chars == 1000


def test_explicit_history_import_is_atomic_idempotent_and_compatible_with_live_dedup(context_store):
    """显式导入一次插入整行，重跑不覆盖；导入身份继续参与正常接收和发送去重。"""
    from agent_bot.sent_messages import SentMessageUncertainError

    store, _, _, now = context_store
    generation = "a" * 32
    events = [
        {"role": "user", "content": "旧用户", "request_id": "old-request",
         "created_at": now[0], "material_ids": ()},
        {"role": "assistant", "content": "旧成功回复", "request_id": "old-request",
         "created_at": now[0] + timedelta(seconds=1), "material_ids": ("file",),
         "delivery_id": "b" * 64, "message_id": "qq-old", "status": "sent"},
        {"role": "assistant", "content": "未确认回复", "request_id": None,
         "created_at": now[0] + timedelta(seconds=2), "material_ids": (),
         "delivery_id": "c" * 64, "message_id": None, "status": "pending"},
    ]
    assert store.import_history("s", generation_id=generation, memory_summary="", events=events)
    assert not store.import_history("s", generation_id="d" * 32, memory_summary="覆盖", events=[])
    assert store.ensure_generation("s") == generation
    assert contents(store) == [("user", "旧用户"), ("assistant", "旧成功回复")]
    store.record_received("s", "旧用户", request_id="old-request")
    assert len(contents(store)) == 2
    assert store.claim_delivery("s", generation, "b" * 64, "old-request", None,
                                "旧成功回复", ("file",)).message_id == "qq-old"
    with pytest.raises(SentMessageUncertainError):
        store.claim_delivery("s", generation, "c" * 64, None, None, "未确认回复", ())
    assert store.pending_deliveries("s", generation) == ("c" * 64,)


def test_invalid_migration_does_not_leave_placeholder_or_partial_messages(context_store):
    """迁移中途发现坏事件必须保留可重跑状态，不能先落一行空会话后跳过历史。"""
    store, _, _, now = context_store
    events = [
        {"role": "user", "content": "有效旧事件", "request_id": "r1",
         "created_at": now[0], "material_ids": ()},
        {"role": "invalid", "content": "错误事件", "request_id": None,
         "created_at": now[0], "material_ids": ()},
    ]
    with pytest.raises(ValueError):
        store.import_history("s", generation_id="a" * 32, memory_summary="", events=events)
    assert store.context("s").generation_id is None
    assert store.import_history("s", generation_id="a" * 32, memory_summary="", events=events[:1])
    assert contents(store) == [("user", "有效旧事件")]


def test_simultaneous_delivery_import_and_normal_append_do_not_lose_messages(context_store):
    """兼容轮次先提交后平台分段到达时，只用真实分段替换兼容整段。"""
    store, _, _, _ = context_store
    store.commit_exchange("s", "用户", "整段兼容回答", request_id="r1")
    generation = store.ensure_generation("s")
    store.record("s", generation, "a" * 64, "r1", "qq-1", "真实片段一", ())
    store.record("s", generation, "b" * 64, "r1", "qq-2", "真实片段二", ())
    assert contents(store) == [("user", "用户"), ("assistant", "真实片段一"),
                               ("assistant", "真实片段二")]


def test_finish_unknown_session_does_not_fabricate_a_confirmation(context_store):
    """不存在的会话或占位不能返回成功外观，也不能隐式创建空历史。"""
    store, _, _, _ = context_store
    with pytest.raises(ValueError, match="was not claimed"):
        store.finish_delivery("unknown", "a" * 64, "qq-1")
    assert store.context("unknown").generation_id is None


def test_pending_delivery_never_becomes_a_success_through_legacy_commit(context_store):
    """已有平台占位但尚未确认时，兼容轮次提交也不能把模型正文伪装为已发消息。"""
    store, _, _, _ = context_store
    generation = store.ensure_generation("s")
    store.record_received("s", "用户", request_id="r1")
    store.claim_delivery("s", generation, "a" * 64, "r1", None, "等待平台确认", ())
    store.commit_exchange("s", "用户", "尚未确认的整段正文", request_id="r1")
    assert contents(store) == [("user", "用户")]
    store.finish_delivery("s", "a" * 64, "qq-1")
    assert contents(store) == [("user", "用户"), ("assistant", "等待平台确认")]


def test_queued_old_generation_may_send_without_entering_new_context(context_store):
    """清空不撤销已经授权的后台发送，首次占位和平台确认仍属于原代际。"""
    store, _, _, _ = context_store
    old = store.ensure_generation("s")
    store.clear("s")
    current = store.ensure_generation("s")
    values = ("s", old, "a" * 64, "queued-drawing", None, "旧后台任务的图片", ("picture",))
    assert store.claim_delivery(*values) is None
    assert store.pending_deliveries("s", old) == ("a" * 64,)
    assert store.pending_deliveries("s", current) == ()
    finished = store.finish_delivery("s", "a" * 64, "qq-old-task")
    assert finished.message_id == "qq-old-task"
    assert store.claim_delivery(*values) == finished
    assert contents(store) == []
    assert store.list_messages("s", current) == store.list_messages("s", old) == ()


def test_history_import_keeps_all_delivery_generations_but_only_current_context(context_store):
    """历史所有代际的发送身份都保留防重，旧正文及旧占位不得进入当前消息流。"""
    from agent_bot.sent_messages import SentMessageUncertainError

    store, _, _, now = context_store
    current, old = "a" * 32, "b" * 32
    events = [
        {"role": "assistant", "content": "旧成功正文", "request_id": "old-sent",
         "created_at": now[0], "material_ids": ("old-image",),
         "delivery_id": "c" * 64, "message_id": "qq-old", "status": "sent",
         "generation_id": old},
        {"role": "assistant", "content": "旧未确认正文", "request_id": "old-pending",
         "created_at": now[0], "material_ids": (), "delivery_id": "d" * 64,
         "message_id": None, "status": "pending", "generation_id": old},
        {"role": "user", "content": "当前提问", "request_id": "current",
         "created_at": now[0], "material_ids": ()},
        {"role": "assistant", "content": "当前回答", "request_id": "current",
         "created_at": now[0], "material_ids": (), "delivery_id": "e" * 64,
         "message_id": "qq-current", "status": "sent"},
    ]
    assert store.import_history("s", generation_id=current, memory_summary="", events=events)
    assert contents(store) == [("user", "当前提问"), ("assistant", "当前回答")]
    assert store.pending_deliveries("s", old) == ("d" * 64,)
    restored = store.claim_delivery("s", old, "c" * 64, "old-sent", None,
                                    "旧成功正文", ("old-image",))
    assert restored.message_id == "qq-old" and restored.material_ids == ("old-image",)
    with pytest.raises(SentMessageUncertainError):
        store.claim_delivery("s", old, "d" * 64, "old-pending", None, "旧未确认正文", ())
    store.finish_delivery("s", "d" * 64, "qq-confirmed-later")
    assert contents(store) == [("user", "当前提问"), ("assistant", "当前回答")]
    assert [message.content for message in store.list_messages("s", current)] == ["当前回答"]


@pytest.mark.parametrize("history_turns", [0, 1])
def test_active_input_never_enters_memory_before_request_finishes(context_store, history_turns):
    """零历史和乱序并发输入都不能提前把当前问题压入摘要，导致模型再次看到自身输入。"""
    from agent_bot.session_context import SessionContextPolicy

    store, _, _, now = context_store
    store.update_policy(SessionContextPolicy(history_turns, 10_000, True, 4000))
    if history_turns:
        for index in (1, 2):
            store.record_received("s", f"并发其他输入{index}", request_id=f"other-{index}",
                                  created_at=now[0] + timedelta(seconds=index))
    store.record_received("s", "CURRENT_MARKER", request_id="current", created_at=now[0])
    view = store.context("s", exclude_request_id="current")
    assert "CURRENT_MARKER" not in view.memory
    assert all("CURRENT_MARKER" not in message.content for message in view.messages)
    compression = store.finish_request("s", "current")
    assert compression is not None
    assert "CURRENT_MARKER" in store.context("s").memory


def test_active_group_protects_received_and_sent_parts_until_terminal_request(context_store):
    """并发压缩保护整个未结束请求组，其他请求不能抢先压缩它的用户输入或真实分段。"""
    from agent_bot.session_context import SessionContextPolicy

    store, _, _, _ = context_store
    store.update_policy(SessionContextPolicy(0, 10_000, True, 4000))
    generation = store.ensure_generation("s")
    store.record_received("s", "ACTIVE_USER", request_id="active")
    store.record("s", generation, "a" * 64, "active", "qq-part", "ACTIVE_PART", ())
    store.record_received("s", "其他输入", request_id="other")
    store.finish_request("s", "other")
    memory = store.context("s", exclude_request_id="active").memory
    assert "ACTIVE_USER" not in memory and "ACTIVE_PART" not in memory
    assert "其他输入" in memory
    store.commit_exchange("s", "ACTIVE_USER", "不应重复保存的整段", request_id="active")
    memory = store.context("s").memory
    assert "ACTIVE_USER" in memory and "ACTIVE_PART" in memory
    assert "不应重复保存的整段" not in memory


def test_finish_failed_request_does_not_invent_success_or_disable_dedup(context_store):
    """失败终态只解除压缩保护，既不伪造助手消息，也不阻断后续合法提交或接收去重。"""
    store, _, _, _ = context_store
    store.record_received("s", "输入", request_id="r1")
    store.finish_request("s", "r1")
    store.record_received("s", "输入", request_id="r1")
    assert contents(store) == [("user", "输入")]
    store.commit_exchange("s", "输入", "已确认兼容回复", request_id="r1")
    assert contents(store) == [("user", "输入"), ("assistant", "已确认兼容回复")]


@pytest.mark.parametrize("status", ["sent", "pending"])
def test_old_imported_delivery_blocks_late_commit_without_received_receipt(context_store, status):
    """旧队列恢复提交不能因旧输入回执未迁移而向当前代际补造用户和助手消息。"""
    store, _, _, now = context_store
    store.import_history("s", generation_id="a" * 32, memory_summary="", events=[{
        "role": "assistant", "content": "旧队列投递内容", "request_id": "old-job",
        "created_at": now[0], "material_ids": (), "delivery_id": "c" * 64,
        "message_id": "qq-old" if status == "sent" else None,
        "generation_id": "b" * 32, "status": status,
    }])
    store.commit_exchange("s", "旧任务用户", "旧任务整段摘要", request_id="old-job")
    assert contents(store) == []
    store.record_received("s", "新任务输入", request_id="new-job")
    store.commit_exchange("s", "新任务输入", "新任务回复", request_id="new-job")
    assert contents(store) == [("user", "新任务输入"), ("assistant", "新任务回复")]


def test_compression_preserves_real_speaker_event_id_and_time(context_store):
    """同名群成员不能在压缩时丢掉服务端绑定的身份或原事件时间。"""
    from agent_bot.session_context import SessionContextPolicy

    store, params, _, now = context_store
    store.update_policy(SessionContextPolicy(0, 10_000, True, 4000))
    source = {"speaker_id": "qq:bot:member-a", "scene": "group", "source_kind": "user"}
    store.record_received("s", "哥哥：我喜欢甜食", request_id="r1", source=source)
    with sqlite3.connect(params.sqlite_path) as connection:
        original = json.loads(connection.execute(
            "SELECT messages_json FROM chat_session_context",
        ).fetchone()[0])[0]
    compression = store.finish_request("s", "r1")
    assert compression.events[0].id == original["id"]
    assert compression.events[0].created_at == now[0].replace(tzinfo=None).isoformat(
        timespec="microseconds",
    )
    assert compression.events[0].source == source
    assert compression.events[0].content == "哥哥：我喜欢甜食"
    assert "未验证" in store.context("s").memory


def test_duplicate_received_request_cannot_change_speaker(context_store):
    """同一请求正文相同但真实发言人不同也必须报身份冲突。"""
    store, _, _, _ = context_store
    store.record_received("s", "我喜欢甜食", request_id="r1", source={
        "speaker_id": "member-a", "scene": "group", "source_kind": "user",
    })
    with pytest.raises(ValueError, match="identity conflicts"):
        store.record_received("s", "我喜欢甜食", request_id="r1", source={
            "speaker_id": "member-b", "scene": "group", "source_kind": "user",
        })


def test_imported_plain_summary_is_archived_without_default_model_injection(context_store):
    """旧纯文本无法证明主体与来源，保留原件但不进入模型历史窗口。"""
    store, params, _, _ = context_store
    store.import_history("s", generation_id="a" * 32,
                         memory_summary="哥哥喜欢甜食(旧摘要)", events=[])
    assert store.context("s").memory == ""
    with sqlite3.connect(params.sqlite_path) as connection:
        assert connection.execute(
            "SELECT legacy_memory_summary,memory_summary FROM chat_session_context",
        ).fetchone() == ("哥哥喜欢甜食(旧摘要)", "")


async def test_structured_compression_binds_subject_and_time_to_real_events(context_store):
    """更正及完成状态替换旧摘要，模型不能以昵称或伪造时间改变真实发言人。"""
    from agent_bot.model_router import ContentModelRouter
    from agent_bot.services.chat.memory import ChatMemoryCompressor
    from agent_bot.session_context import SessionContextPolicy

    store, params, _, now = context_store
    store.update_policy(SessionContextPolicy(0, 10_000, True, 4000))
    store.record_received("s", "更正：我不喜欢甜食，之前的部署任务已完成", request_id="r1",
                          source={"speaker_id": "member-b", "scene": "group",
                                  "source_kind": "user"})
    compression = store.finish_request("s", "r1")

    class Client:
        """只替换外部付费模型，实际摘要校验与数据库事务保持真实。"""

        async def complete(self, **options):
            """返回带当前真实事件引用的压缩结果。"""
            return json.dumps({"entries": [
                {"kind": "correction", "subject_id": "member-b", "content": "不喜欢甜食",
                 "status": "active", "source_event_refs": [compression.events[0].id],
                 "observed_at": "2099-01-01"},
                {"kind": "task", "subject_id": "member-b", "content": "部署任务已完成",
                 "status": "completed", "source_event_refs": [compression.events[0].id]},
            ]}, ensure_ascii=False)

    compressor = ChatMemoryCompressor(client=Client(), store=store,
                                     model_router=ContentModelRouter("text", "v", "i", "a"),
                                     max_tokens=512)
    await compressor.compress(compression)
    with sqlite3.connect(params.sqlite_path) as connection:
        facts = json.loads(connection.execute(
            "SELECT memory_facts_json FROM chat_session_context",
        ).fetchone()[0])
    assert [entry["kind"] for entry in facts["entries"]] == ["correction", "task"]
    assert all(entry["subject_id"] == "member-b" for entry in facts["entries"])
    assert all(entry["observed_at"] == now[0].replace(tzinfo=None).isoformat(
        timespec="microseconds",
    ) for entry in facts["entries"])
    assert facts["entries"][1]["status"] == "completed"
    assert "不喜欢甜食" in store.context("s").memory


@pytest.mark.parametrize("forgery", ["speaker", "event", "assistant_preference"])
async def test_compressor_rejects_unverified_personal_attribution(context_store, forgery):
    """伪造来源、把群友当当前用户或把助手复述当本人偏好都不能覆盖安全占位。"""
    from dataclasses import replace

    from agent_bot.model_router import ContentModelRouter
    from agent_bot.services.chat.memory import ChatMemoryCompressor
    from agent_bot.session_context import SessionContextPolicy

    store, params, _, _ = context_store
    store.update_policy(SessionContextPolicy(0, 10_000, True, 4000))
    store.record_received("s", "哥哥：我喜欢甜食", request_id="r1", source={
        "speaker_id": "member-a", "scene": "group", "source_kind": "user",
    })
    compression = store.finish_request("s", "r1")
    if forgery == "assistant_preference":
        compression = replace(compression, events=(replace(compression.events[0],
                                                          role="assistant"),))

    class Client:
        """付费模型替身仅返回不可信的结构，不替换生产校验。"""

        async def complete(self, **options):
            """尝试把另一个人的话或伪造事件提升为个人偏好。"""
            return json.dumps({"entries": [{
                "kind": "preference", "subject_id": "member-b" if forgery == "speaker"
                else "member-a", "content": "FORGED_PREFERENCE", "status": "active",
                "source_event_refs": ["fake-event" if forgery == "event"
                                      else compression.events[0].id],
            }]})

    await ChatMemoryCompressor(client=Client(), store=store,
                               model_router=ContentModelRouter("text", "v", "i", "a"),
                               max_tokens=512).compress(compression)
    assert "FORGED_PREFERENCE" not in store.context("s").memory
    with sqlite3.connect(params.sqlite_path) as connection:
        assert json.loads(connection.execute(
            "SELECT memory_facts_json FROM chat_session_context",
        ).fetchone()[0])["source_integrity"] == "unverified"


def test_summary_memory_reference_is_rechecked_before_commit_and_each_injection(context_store):
    """压缩期间遗忘和提交后遗忘都不能让旧引用正文重新进入模型。"""
    from agent_bot.session_context import SessionContextPolicy, SessionContextStore
    from agent_bot.state import MemorySummary

    _, params, _, _ = context_store
    active = {1, 2}

    def filter_entries(connection, session_id, entries):
        """替代外部生命周期服务，沿用实际同一数据库连接验证调用边界。"""
        assert connection is not None and session_id == "s"
        return tuple(entry for entry in entries
                     if all(ref["id"] in active for ref in entry["memory_refs"]))

    store = SessionContextStore(params, policy=SessionContextPolicy(0, 10_000, True, 4000),
                                summary_filter=filter_entries)
    store.record_received("s", "用于触发摘要", request_id="r1")
    compression = store.finish_request("s", "r1")
    active.remove(1)
    assert store.apply_memory(compression, MemorySummary(tuple({
        "kind": "fact", "subject_id": "unknown", "content": f"SECRET_{number}",
        "memory_refs": [{"id": number, "revision": 1}], "source_event_refs": [],
    } for number in (1, 2))))
    assert "SECRET_1" not in store.context("s").memory
    assert "SECRET_2" in store.context("s").memory
    active.remove(2)
    assert store.context("s").memory == ""


async def test_chat_receive_binds_server_sender_not_identical_display_names(context_store):
    """共享群内昵称相同的两个发言人仍使用真实平台身份分别保存。"""
    from agent_bot.model_router import ContentModelRouter
    from agent_bot.models import ChatContext
    from agent_bot.services.chat.service import ChatService
    from agent_bot.services.tagged_memory import MemoryIdentity
    from agent_bot.session_delivery import SessionDeliveryStore

    store, params, _, _ = context_store
    service = ChatService(
        client=object(), store=store, session_delivery=SessionDeliveryStore(lambda: store),
        system_prompt="", max_input_chars=100, cooldown_seconds=0, max_concurrent_requests=1,
        model_router=ContentModelRouter("t", "v", "i", "a"),
        session_secret="test-session-secret",
    )
    for sender in ("12345", "45678"):
        await service.receive_queued("s", "我喜欢甜食", request_id=f"r{sender}",
                                     chat_context=ChatContext(
                                         scene="group", bot_id="11111", group_id="22222",
                                         sender_id=sender, sender_name="哥哥",
                                     ))
    with sqlite3.connect(params.sqlite_path) as connection:
        events = json.loads(connection.execute(
            "SELECT messages_json FROM chat_session_context",
        ).fetchone()[0])
    assert events[0]["source"]["speaker_id"] != events[1]["source"]["speaker_id"]
    assert all(event["source"]["scene"] == "group" for event in events)
    assert all("哥哥" not in event["source"]["speaker_id"] for event in events)
    assert events[0]["source"]["speaker_id"] == MemoryIdentity(
        bot_id="11111", user_id="12345", session_id="s",
    ).owner("user", "test-session-secret")


async def test_compression_preserves_platform_source_reference(context_store):
    """平台事件引用随结构摘要保存，供长期记忆遗忘按真实证据定位，而非靠文本匹配。"""
    from agent_bot.model_router import ContentModelRouter
    from agent_bot.services.chat.memory import ChatMemoryCompressor
    from agent_bot.session_context import SessionContextPolicy

    store, params, _, _ = context_store
    store.update_policy(SessionContextPolicy(0, 10_000, True, 4000))
    store.record_received("s", "我喜欢茶", request_id="request-1", source={
        "speaker_id": "member-a", "scene": "group", "source_kind": "user",
        "source_event_id": "qq:bot:message-1",
    })
    compression = store.finish_request("s", "request-1")

    class Client:
        """返回实际平台引用，不伪造服务端未提供的证据。"""

        async def complete(self, **options):
            """引用平台来源而非会话内部 UUID。"""
            return json.dumps({"entries": [{"kind": "preference", "subject_id": "member-a",
                                           "content": "喜欢茶", "status": "active",
                                           "source_event_refs": ["qq:bot:message-1"]}]})

    await ChatMemoryCompressor(client=Client(), store=store,
                               model_router=ContentModelRouter("t", "v", "i", "a"),
                               max_tokens=512).compress(compression)
    with sqlite3.connect(params.sqlite_path) as connection:
        entries = json.loads(connection.execute(
            "SELECT memory_facts_json FROM chat_session_context",
        ).fetchone()[0])["entries"]
    assert entries[0]["source_event_refs"] == ["qq:bot:message-1"]
    assert entries[0]["source_request_refs"] == ["request-1"]


def test_oversize_event_marks_missing_summary_without_creating_partial_fact(context_store):
    """超长原文应保留缺失标记与来源，不能静默为空或截成可能反义的片段。"""
    from agent_bot.session_context import SessionContextPolicy

    store, _, _, _ = context_store
    store.update_policy(SessionContextPolicy(0, 10_000, True, 500))
    store.record_received("s", "不可截断的事实" * 500, request_id="r1")
    store.finish_request("s", "r1")
    memory = store.context("s").memory
    assert "原文过长" in memory
    assert "不可截断的事实" not in memory


async def test_direct_tool_event_binds_source_only_inside_its_session(context_store):
    """同步事件工具租约绑定真实平台来源，退出租约后不会污染下一条服务输入。"""
    from types import SimpleNamespace

    from agent_bot.config import RuntimeConfig
    from agent_bot.integrations.onebot.tooling import ChatToolSessionManager
    from agent_bot.model_router import ContentModelRouter
    from agent_bot.services.chat.service import ChatService
    from agent_bot.services.tool_sessions import ToolSessionStore
    from agent_bot.session_delivery import SessionDeliveryStore

    store, params, _, _ = context_store
    config = RuntimeConfig(chat_session_secret="source-test-session-secret-32-characters",
                           onebot_v11_access_token="source-test-onebot-token-32-characters",
                           chat_mcp_enabled=True)
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=3)
    manager = ChatToolSessionManager(
        config_provider=lambda: config, store_provider=lambda: sessions,
        naraka_service_provider=lambda: None, group_member_refresher=lambda *_: None,
        memory_store_provider=lambda: object(),
    )
    service = ChatService(
        client=object(), store=store, session_delivery=SessionDeliveryStore(lambda: store),
        session_secret=config.chat_session_secret.get_secret_value(), system_prompt="",
        max_input_chars=100, cooldown_seconds=0, max_concurrent_requests=1,
        model_router=ContentModelRouter("t", "v", "i", "a"),
    )
    event = SimpleNamespace(user_id=10001, message_id=111, time=1_700_000_000,
                            get_plaintext=lambda: "我喜欢茶",
                            original_message=SimpleNamespace(extract_plain_text=lambda: "我喜欢茶"))
    async with manager.open_chat_session(SimpleNamespace(self_id="90001"), event, "s"):
        await service.receive_queued("s", "我喜欢茶", request_id="direct")
        await service.receive_queued("other", "不同会话", request_id="other")
    await service.receive_queued("s", "下一条", request_id="later")
    with sqlite3.connect(params.sqlite_path) as connection:
        events = json.loads(connection.execute(
            "SELECT messages_json FROM chat_session_context WHERE session_id='s'",
        ).fetchone()[0])
        other = json.loads(connection.execute(
            "SELECT messages_json FROM chat_session_context WHERE session_id='other'",
        ).fetchone()[0])
    assert events[0]["source"]["source_event_id"] == "qq:90001:111"
    assert events[0]["created_at"] == "2023-11-14T22:13:20.000000"
    assert events[1]["source"] == {}
    assert other[0]["source"] == {}


@pytest.mark.parametrize("terminal_status", ["completed", "cancelled"])
def test_summary_budget_keeps_open_task_before_completed_task(context_store, terminal_status):
    """同类条目预算不足时先保留未完成任务，不能让旧完成或取消记录挤掉待办。"""
    from agent_bot.session_context import SessionContextPolicy
    from agent_bot.state import MemorySummary

    store, _, _, _ = context_store
    store.update_policy(SessionContextPolicy(0, 10_000, True, 250))
    store.record_received("s", "任务变化", request_id="r1")
    compression = store.finish_request("s", "r1")
    store.apply_memory(compression, MemorySummary((
        {"kind": "task", "subject_id": "member", "content": "完成记录" * 25,
         "status": terminal_status, "source_event_refs": [], "memory_refs": []},
        {"kind": "task", "subject_id": "member", "content": "待办记录" * 25,
         "status": "open", "source_event_refs": [], "memory_refs": []},
    )))
    memory = store.context("s").memory
    assert "待办记录" in memory
    assert "完成记录" not in memory


@pytest.mark.parametrize("task_status,label", [
    ("open", "任务状态：未完成"),
    ("completed", "任务状态：已完成"),
    ("cancelled", "任务状态：已取消"),
])
async def test_structured_task_state_survives_actual_summary_injection(
    context_store, task_status, label,
):
    """摘要正文完全相同时仍把任务状态注入模型，禁止完成或取消后重新当作待办。"""
    from agent_bot.model_router import ContentModelRouter
    from agent_bot.services.chat.memory import ChatMemoryCompressor
    from agent_bot.services.chat.prompting import ChatPromptBuilder
    from agent_bot.session_context import SessionContextPolicy

    store, _, _, _ = context_store
    store.update_policy(SessionContextPolicy(0, 10_000, True, 4000))
    store.record_received("s", "部署服务任务状态变化", request_id="r1", source={
        "speaker_id": "member", "scene": "private", "source_kind": "user",
    })
    compression = store.finish_request("s", "r1")

    class Client:
        """只替代外部模型，结构验证、保存和提示注入均使用真实实现。"""

        async def complete(self, **options):
            """任务正文相同，业务意义只由结构化状态字段区分。"""
            return json.dumps({"entries": [{"kind": "task", "subject_id": "member",
                                           "content": "部署服务", "status": task_status,
                                           "source_event_refs": [compression.events[0].id]}]})

    await ChatMemoryCompressor(client=Client(), store=store,
                               model_router=ContentModelRouter("t", "v", "i", "a"),
                               max_tokens=512).compress(compression)
    messages = ChatPromptBuilder(system_prompt="", max_input_chars=100).messages(
        "接下来做什么", conversation_context=store.context("s"),
    )
    injected = next(message.content for message in messages
                    if "[会话心智记忆资料开始]" in message.content)
    assert "部署服务" in injected
    assert label in injected


async def test_legacy_queued_prompt_never_becomes_user_source_via_chat_context(context_store):
    """旧任务缺原话时必须显式未知，防止ChatContext把混合引用重新提升为本人资料。"""
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent_bot.integrations.onebot.queue_processor import ConversationQueueProcessor
    from agent_bot.model_router import ContentModelRouter
    from agent_bot.models import ChatContext
    from agent_bot.services.chat.service import ChatService
    from agent_bot.services.conversation_queue import ConversationJob
    from agent_bot.session_delivery import SessionDeliveryStore

    store, params, _, _ = context_store
    service = ChatService(
        client=object(), store=store, session_secret="test-secret",
        session_delivery=SessionDeliveryStore(lambda: store), system_prompt="",
        max_input_chars=100, cooldown_seconds=0, max_concurrent_requests=1,
        model_router=ContentModelRouter("t", "v", "i", "a"),
    )

    @asynccontextmanager
    async def lease():
        """来源边界不需要完整插件或平台连接。"""
        yield

    processor = ConversationQueueProcessor(
        config_provider=lambda: SimpleNamespace(), chat_service_provider=lambda: service,
        runtime_lease=lease, tool_session_provider=lease, bot_provider=lambda _: None,
        reply_composer=None, reply_gateway=None, operation_store_provider=lambda: None,
        image_submitter=AsyncMock(), workflow_service_provider=lambda: None,
        receive_queued=service.receive_queued,
    )
    job = ConversationJob(job_id="old", request_id="qq-old", created_at=1,
                          session_id="s", bot_id="90001", user_id="10001", scene="private",
                          prompt="引用群友：我喜欢甜食", chat_context=ChatContext(
                              bot_id="90001", sender_id="10001", scene="private",
                          ))
    await processor._receive_original(job)
    with sqlite3.connect(params.sqlite_path) as connection:
        source = json.loads(connection.execute(
            "SELECT messages_json FROM chat_session_context",
        ).fetchone()[0])[0]["source"]
    assert source["source_kind"] == "unknown"


@pytest.mark.parametrize("field", ["job", "event"])
def test_source_text_rejects_embedded_nul(context_store, field):
    """原话字段有独立边界，不能把NUL送入证据摘要或持久任务。"""
    from agent_bot.services.conversation_queue import ConversationJob

    store, _, _, _ = context_store
    with pytest.raises(ValueError):
        if field == "job":
            ConversationJob(job_id="job", request_id="qq-job", created_at=1,
                            session_id="s", bot_id="90001", user_id="10001", scene="private",
                            prompt="正常问题", memory_source_text="坏\0原话")
        else:
            store.record_received("s", "正常问题", request_id="r1", source={
                "source_kind": "user", "source_text": "坏\0原话",
            })


async def test_current_model_request_can_match_summary_subject_without_nickname_guess(
    context_store,
):
    """当前模型请求获得真实发言人与摘要匿名主体的可信映射，不靠同名群友猜身份。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from agent_bot.model_router import ContentModelRouter
    from agent_bot.models import ChatContext
    from agent_bot.services.chat.service import ChatService
    from agent_bot.services.tagged_memory import MemoryIdentity
    from agent_bot.session_delivery import SessionDeliveryStore

    store, _, _, _ = context_store
    client = SimpleNamespace(complete=AsyncMock(return_value="回应"))
    service = ChatService(
        client=client, store=store, session_secret="test-secret",
        session_delivery=SessionDeliveryStore(lambda: store), system_prompt="",
        max_input_chars=100, cooldown_seconds=0, max_concurrent_requests=1,
        model_router=ContentModelRouter("t", "v", "i", "a"),
    )
    await service.ask("s", "问候", chat_context=ChatContext(
        scene="group", group_id="20000", bot_id="90001", sender_id="10001", sender_name="哥哥",
    ))
    subject = MemoryIdentity(bot_id="90001", user_id="10001", session_id="s").owner(
        "user", "test-secret",
    )
    trusted = [message.content for message in client.complete.await_args.kwargs["messages"]
               if message.role == "system"]
    assert any(subject in content for content in trusted)
