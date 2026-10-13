from __future__ import annotations

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pymysql.err import OperationalError

from agent_bot.memory_store import (
    ChatMemorySchema,
    ChatMemorySchemaError,
    ConversationMemoryPolicy,
    GroupContextPolicy,
    MySqlConversationMemoryStore,
    MySqlRecentGroupContextStore,
)
from agent_bot.models import ChatMessage
from agent_bot.settings import DatabaseParams
from agent_bot.state import ConversationStore, external_session_id

_EXPECTED_SCHEMA = {
    "chat_memory_session": {
        "columns": {
            "session_id",
            "generation_id",
            "memory_summary",
            "memory_revision",
            "compacted_through_turn_id",
            "expires_at",
            "create_time",
            "update_time",
        },
        "indexes": {"PRIMARY", "idx_chat_memory_session_expires"},
    },
    "chat_memory_turn": {
        "columns": {
            "id",
            "session_id",
            "request_id",
            "user_content",
            "assistant_content",
            "create_time",
        },
        "indexes": {"PRIMARY", "idx_chat_memory_turn_session_id", "uk_chat_memory_turn_request"},
    },
    "chat_memory_receipt": {
        "columns": {"session_id", "request_id", "expires_at"},
        "indexes": {"PRIMARY", "idx_chat_memory_receipt_expires"},
    },
    "chat_group_context": {
        "columns": {
            "scope_id",
            "messages_json",
            "expires_at",
            "create_time",
            "update_time",
        },
        "indexes": {"PRIMARY", "idx_chat_group_context_expires"},
    },
}


class _SchemaCursor:
    def __init__(self) -> None:
        self.created_tables: list[str] = []
        self.created_statements: list[str] = []
        self.upgrades: list[str] = []
        self.generation_nullable = False
        self.legacy_sessions: list[dict[str, object]] = []
        self.schema = {
            name: {key: set(values) for key, values in definition.items()}
            for name, definition in _EXPECTED_SCHEMA.items()
        }
        self.foreign_keys = {
            (
                "chat_memory_turn",
                "fk_chat_memory_turn_session",
            )
        }
        self.dropped_foreign_keys: list[tuple[str, str]] = []
        self._rows: list[dict[str, str]] = []

    def __enter__(self) -> _SchemaCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        return None

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> int:
        """模拟记忆结构升级，索引改名只改变元数据并记录操作次数。"""
        normalized = " ".join(sql.split())
        if normalized.startswith("CREATE TABLE IF NOT EXISTS"):
            table_name = normalized.split("`")[1]
            self.created_tables.append(table_name)
            self.created_statements.append(normalized)
            self._rows = []
        elif "INFORMATION_SCHEMA.TABLE_CONSTRAINTS" in normalized:
            self._rows = [
                {"TABLE_NAME": table_name, "CONSTRAINT_NAME": constraint_name}
                for table_name, constraint_name in sorted(self.foreign_keys)
            ]
        elif normalized.startswith("ALTER TABLE") and "DROP FOREIGN KEY" in normalized:
            identifiers = normalized.split("`")
            dropped = (identifiers[1], identifiers[3])
            self.foreign_keys.remove(dropped)
            self.dropped_foreign_keys.append(dropped)
            self._rows = []
        elif normalized.startswith("ALTER TABLE") and "RENAME INDEX" in normalized:
            identifiers = normalized.split("`")
            table, old, new = identifiers[1], identifiers[3], identifiers[5]
            self.schema[table]["indexes"].remove(old)
            self.schema[table]["indexes"].add(new)
            self.upgrades.append(normalized)
            self._rows = []
        elif normalized.startswith("ALTER TABLE") and "ADD COLUMN `request_id`" in normalized:
            self.upgrades.append(normalized)
            self.schema["chat_memory_turn"]["columns"].add("request_id")
        elif normalized.startswith("ALTER TABLE") and "ADD UNIQUE KEY" in normalized:
            self.upgrades.append(normalized)
            self.schema["chat_memory_turn"]["indexes"].add("uk_chat_memory_turn_request")
        elif normalized.startswith("ALTER TABLE") and "ADD COLUMN `generation_id`" in normalized:
            self.upgrades.append(normalized)
            self.schema["chat_memory_session"]["columns"].add("generation_id")
            self.generation_nullable = True
        elif normalized.startswith("UPDATE") and "SET generation_id = REPLACE(UUID()" in normalized:
            for index, row in enumerate(self.legacy_sessions, start=1):
                if row.get("generation_id") is None:
                    row["generation_id"] = f"{index:032x}"
        elif normalized.startswith("ALTER TABLE") and "MODIFY COLUMN `generation_id`" in normalized:
            self.upgrades.append(normalized)
            assert all(row.get("generation_id") for row in self.legacy_sessions)
            self.generation_nullable = False
        elif "INFORMATION_SCHEMA.COLUMNS" in normalized:
            table_name = str(parameters[1])
            self._rows = [
                {
                    "COLUMN_NAME": name,
                    "IS_NULLABLE": "YES"
                    if name == "generation_id" and self.generation_nullable
                    else "NO",
                }
                for name in sorted(self.schema[table_name]["columns"])
            ]
        elif "INFORMATION_SCHEMA.STATISTICS" in normalized:
            table_name = str(parameters[1])
            self._rows = [
                {"INDEX_NAME": name} for name in sorted(self.schema[table_name]["indexes"])
            ]
        else:
            raise AssertionError(f"unexpected schema SQL: {normalized}")
        return 0

    def fetchall(self) -> list[dict[str, str]]:
        return self._rows


class _SchemaConnection:
    def __init__(self) -> None:
        self.cursor_instance = _SchemaCursor()
        self.committed = False

    def cursor(self) -> _SchemaCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.committed = True

    def close(self) -> None:
        return None


class _SqliteCursor:
    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._cursor = cursor

    @property
    def lastrowid(self) -> int:
        value = self._cursor.lastrowid
        assert value is not None
        return value

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def __enter__(self) -> _SqliteCursor:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._cursor.close()

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> int:
        normalized = sql.replace("%s", "?").replace(" FOR UPDATE", "")
        normalized = normalized.replace("INSERT IGNORE", "INSERT OR IGNORE")
        sqlite_parameters = tuple(
            value.isoformat(sep=" ") if isinstance(value, datetime) else value
            for value in parameters
        )
        self._cursor.execute(normalized, sqlite_parameters)
        return self.rowcount

    def fetchone(self) -> sqlite3.Row | None:
        return self._cursor.fetchone()

    def fetchall(self) -> list[sqlite3.Row]:
        return self._cursor.fetchall()


class _SqliteConnection:
    def __init__(self, path: Path) -> None:
        self._connection = sqlite3.connect(path, timeout=5)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")

    def cursor(self) -> _SqliteCursor:
        return _SqliteCursor(self._connection.cursor())

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


@pytest.fixture
def memory_database(tmp_path: Path) -> Callable[..., Any]:
    path = tmp_path / "chat-memory.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE chat_memory_session (
            session_id TEXT PRIMARY KEY,
            generation_id TEXT NOT NULL,
            memory_summary TEXT NOT NULL DEFAULT '',
            memory_revision INTEGER NOT NULL DEFAULT 0,
            compacted_through_turn_id INTEGER NOT NULL DEFAULT 0,
            expires_at TEXT NOT NULL,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL
        );
        CREATE TABLE chat_memory_turn (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            request_id TEXT NULL,
            user_content TEXT NOT NULL,
            assistant_content TEXT NOT NULL,
            create_time TEXT NOT NULL
        );
        CREATE INDEX idx_chat_memory_turn_session_id
            ON chat_memory_turn(session_id, id);
        CREATE UNIQUE INDEX uk_chat_memory_turn_request
            ON chat_memory_turn(session_id, request_id);
        CREATE TABLE chat_memory_receipt (
            session_id TEXT NOT NULL,
            request_id TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (session_id, request_id)
        );
        CREATE TABLE chat_group_context (
            scope_id TEXT PRIMARY KEY,
            messages_json TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            create_time TEXT NOT NULL,
            update_time TEXT NOT NULL
        );
        """
    )
    connection.close()
    return lambda **kwargs: _SqliteConnection(path)


def _store(
    connect: Callable[..., Any],
    *,
    history_turns: int = 2,
    memory_enabled: bool = False,
    max_history_chars: int = 1000,
    clock: Callable[[], datetime] | None = None,
) -> MySqlConversationMemoryStore:
    return MySqlConversationMemoryStore(
        DatabaseParams(
            host="127.0.0.1",
            port=3306,
            user="test",
            password="test",
            database="test",
        ),
        policy=ConversationMemoryPolicy(
            history_turns=history_turns,
            max_history_chars=max_history_chars,
            ttl_seconds=3600,
            memory_enabled=memory_enabled,
            max_memory_chars=100,
        ),
        connect=connect,
        clock=clock or (lambda: datetime(2026, 9, 15, tzinfo=UTC)),
    )


def test_memory_schema_creates_and_validates_all_tables() -> None:
    connection = _SchemaConnection()
    schema = ChatMemorySchema(
        DatabaseParams(
            host="127.0.0.1",
            port=3306,
            user="test",
            password="test",
            database="test",
        ),
        connect=lambda **kwargs: connection,
    )

    schema.initialize()

    assert connection.cursor_instance.created_tables == [
        "chat_memory_session",
        "chat_memory_turn",
        "chat_memory_receipt",
        "chat_group_context",
    ]
    assert all(
        "FOREIGN KEY" not in statement
        for statement in connection.cursor_instance.created_statements
    )
    assert connection.cursor_instance.dropped_foreign_keys == [
        (
            "chat_memory_turn",
            "fk_chat_memory_turn_session",
        )
    ]
    assert connection.cursor_instance.foreign_keys == set()
    assert connection.committed is True


def test_memory_schema_renames_legacy_indexes_without_duplicating_constraints() -> None:
    """改表名遗留的 dispatch 索引原位升级，多次启动不会重建索引或丢失历史。"""
    connection = _SchemaConnection()
    cursor = connection.cursor_instance
    for definition in cursor.schema.values():
        definition["indexes"] = {
            name.replace("idx_chat_", "idx_dispatch_chat_").replace("uk_chat_", "uk_dispatch_chat_")
            for name in definition["indexes"]
        }
    cursor.legacy_sessions = [{"session_id": "keep", "memory_summary": "保留历史"}]
    schema = ChatMemorySchema(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=lambda **kwargs: connection,
    )
    schema.initialize()
    schema.initialize()
    assert len(cursor.upgrades) == 5
    assert all("RENAME INDEX" in operation for operation in cursor.upgrades)
    assert cursor.legacy_sessions == [{"session_id": "keep", "memory_summary": "保留历史"}]


def test_existing_current_index_does_not_conflict_with_legacy_duplicate() -> None:
    """当前幂等索引已存在时，不把旧重复索引改成同名索引。"""
    connection = _SchemaConnection()
    connection.cursor_instance.schema["chat_memory_turn"]["indexes"].add(
        "uk_dispatch_chat_memory_turn_request"
    )
    schema = ChatMemorySchema(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=lambda **kwargs: connection,
    )
    schema.initialize()
    assert connection.cursor_instance.upgrades == []


@pytest.mark.parametrize(
    "kind,name",
    [
        ("columns", "memory_summary"),
        ("indexes", "idx_chat_memory_session_expires"),
    ],
)
def test_schema_error_identifies_missing_structure(kind: str, name: str) -> None:
    """无法迁移的结构问题只公开表、字段和索引名，便于定位启动失败。"""
    connection = _SchemaConnection()
    connection.cursor_instance.schema["chat_memory_session"][kind].remove(name)
    schema = ChatMemorySchema(
        DatabaseParams(
            host="localhost", port=3306, user="test", password="hidden-db-key", database="test"
        ),
        connect=lambda **kwargs: connection,
    )
    with pytest.raises(ChatMemorySchemaError) as caught:
        schema.initialize()
    assert "chat_memory_session" in str(caught.value)
    assert name in str(caught.value)
    assert "hidden-db-key" not in str(caught.value)


def test_memory_schema_upgrades_old_turns_with_nullable_request_id() -> None:
    """兼容升级仅添加可空列和唯一键，多条旧 NULL 记录仍合法。"""
    connection = _SchemaConnection()
    cursor = connection.cursor_instance
    cursor.schema["chat_memory_turn"]["columns"].remove("request_id")
    cursor.schema["chat_memory_turn"]["indexes"].remove("uk_chat_memory_turn_request")
    schema = ChatMemorySchema(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=lambda **kwargs: connection,
    )
    schema.initialize()
    schema.initialize()
    assert len(cursor.upgrades) == 2
    assert " NULL " in cursor.upgrades[0]
    assert "UNIQUE KEY" in cursor.upgrades[1]


@pytest.mark.parametrize("interrupted", [False, True])
def test_schema_generation_upgrade_preserves_legacy_memory_and_existing_identity(
    interrupted: bool,
) -> None:
    """旧摘要原位保留，首次升级及中断重试都不会重建已存在的代际。"""
    connection = _SchemaConnection()
    cursor = connection.cursor_instance
    cursor.legacy_sessions = [
        {"session_id": "old-1", "memory_summary": "保留摘要一", "generation_id": None},
        {"session_id": "old-2", "memory_summary": "保留摘要二", "generation_id": "f" * 32},
    ]
    if interrupted:
        cursor.generation_nullable = True
    else:
        cursor.schema["chat_memory_session"]["columns"].remove("generation_id")
    schema = ChatMemorySchema(
        DatabaseParams(host="localhost", port=3306, user="test", password="test", database="test"),
        connect=lambda **kwargs: connection,
    )
    schema.initialize()
    before = [dict(row) for row in cursor.legacy_sessions]
    schema.initialize()
    assert cursor.legacy_sessions == before
    assert [row["memory_summary"] for row in before] == ["保留摘要一", "保留摘要二"]
    assert len(str(before[0]["generation_id"])) == 32
    assert before[0]["generation_id"] != before[1]["generation_id"]
    assert before[1]["generation_id"] == "f" * 32
    assert cursor.generation_nullable is False


def test_conversation_memory_survives_store_reconstruction(memory_database) -> None:
    first = _store(memory_database)
    first.commit_exchange("qq_session", "小明：你好", "你好")

    second = _store(memory_database)

    assert second.context("qq_session").messages == (
        ChatMessage(role="user", content="小明：你好"),
        ChatMessage(role="assistant", content="你好"),
    )


def test_commit_request_id_deduplicates_after_reconstruction_and_trimming(memory_database) -> None:
    """重试已裁剪的轮次不能再次写入或推动摘要版本。"""
    first = _store(memory_database, history_turns=1, memory_enabled=True)
    first.commit_exchange("qq_session", "u1", "a1", request_id="job-1")
    first.commit_exchange("qq_session", "u2", "a2", request_id="job-2")
    before = first.context("qq_session")
    second = _store(memory_database, history_turns=1, memory_enabled=True)
    assert second.commit_exchange("qq_session", "u1", "a1", request_id="job-1") is None
    assert second.context("qq_session") == before
    second.commit_exchange("other", "u1", "a1", request_id="job-1")
    assert len(second.context("other").messages) == 2


@pytest.mark.parametrize("cleanup", ["context", "cleanup", "clear"])
def test_request_receipt_survives_history_expiry_and_clear(memory_database, cleanup: str) -> None:
    """会话清空或历史过期不允许仍处于重试窗口的任务恢复旧历史。"""
    now = [datetime(2026, 9, 15, tzinfo=UTC)]
    store = _store(memory_database, clock=lambda: now[0])
    store.commit_exchange("qq_session", "u", "a", request_id="job-1")
    now[0] = datetime(2026, 9, 15, 2, tzinfo=UTC)
    if cleanup == "clear":
        store.clear("qq_session")
    elif cleanup == "cleanup":
        store.cleanup_expired()
    else:
        store.context("qq_session")
    store.commit_exchange("qq_session", "u", "a", request_id="job-1")
    assert store.context("qq_session").messages == ()
    now[0] = datetime(2026, 9, 17, tzinfo=UTC)
    store.cleanup_expired()
    store.commit_exchange("qq_session", "new", "reply", request_id="job-1")
    assert [message.content for message in store.context("qq_session").messages] == ["new", "reply"]


def test_concurrent_same_request_commits_only_one_exchange(memory_database) -> None:
    """两个连接并发提交相同请求时只有一轮历史。"""
    store = _store(memory_database)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(store.commit_exchange, "qq_session", "u", "a", request_id="job")
            for _ in range(2)
        ]
        for future in futures:
            future.result()
    assert len(store.context("qq_session").messages) == 2


def test_failed_turn_insert_rolls_back_request_receipt(memory_database, monkeypatch) -> None:
    """历史提交失败必须同时回滚回执，让下一次重试可以真正提交。"""
    execute = _SqliteCursor.execute
    failed = False

    def fail_turn_once(self, sql, parameters=()):
        """在同一真实 SQLite 事务中模拟一次轮次写入错误。"""
        nonlocal failed
        if "INSERT" in sql and "`chat_memory_turn`" in sql and not failed:
            failed = True
            raise OperationalError("turn insert failed")
        return execute(self, sql, parameters)

    monkeypatch.setattr(_SqliteCursor, "execute", fail_turn_once)
    store = _store(memory_database)
    with pytest.raises(RuntimeError, match="failed to commit"):
        store.commit_exchange("s", "u", "a", request_id="job")
    store.commit_exchange("s", "u", "a", request_id="job")
    assert len(store.context("s").messages) == 2


@pytest.mark.parametrize("request_id", ["", "x" * 129, " invalid", "invalid\n"])
def test_commit_rejects_unbounded_request_identifiers(memory_database, request_id: str) -> None:
    """无效请求编号在数据库写入前被拒绝。"""
    store = _store(memory_database)
    with pytest.raises(ValueError, match="request id"):
        store.commit_exchange("s", "u", "a", request_id=request_id)
    assert store.context("s").messages == ()


def test_legacy_commits_without_request_id_remain_distinct(memory_database) -> None:
    """旧调用没有请求编号时保持逐次追加语义。"""
    store = _store(memory_database)
    store.commit_exchange("qq_session", "u", "a")
    store.commit_exchange("qq_session", "u", "a")
    assert len(store.context("qq_session").messages) == 4


def test_in_memory_store_receipts_outlive_history_and_reset() -> None:
    """内存仓库与数据库仓库保持同样的重试去重契约。"""
    now = [0.0]
    store = ConversationStore(history_turns=1, max_sessions=2, ttl_seconds=1, clock=lambda: now[0])
    store.commit_exchange("s", "u1", "a1", request_id="job-1")
    store.commit_exchange("s", "u2", "a2", request_id="job-2")
    store.commit_exchange("s", "u1", "a1", request_id="job-1")
    assert [message.content for message in store.context("s").messages] == ["u2", "a2"]
    store.clear("s")
    now[0] = 2
    store.commit_exchange("s", "u1", "a1", request_id="job-1")
    assert store.context("s").messages == ()


def test_stale_compression_cannot_overwrite_newer_summary(memory_database) -> None:
    store = _store(memory_database, history_turns=1, memory_enabled=True)
    store.commit_exchange("qq_session", "u1", "a1")
    old = store.commit_exchange("qq_session", "u2", "a2")
    new = store.commit_exchange("qq_session", "u3", "a3")

    assert old is not None
    assert new is not None
    assert store.apply_memory(old, "旧摘要") is False
    assert store.apply_memory(new, "新摘要") is True
    assert store.context("qq_session").memory == "新摘要"


@pytest.mark.parametrize("recreate", ["clear", "expire", "cleanup"])
def test_old_compression_cannot_overwrite_recreated_database_session(
    memory_database,
    recreate: str,
) -> None:
    """旧会话摘要即使版本号与重建会话相同，也不能跨代际写回。"""
    now = [datetime(2026, 9, 15, tzinfo=UTC)]
    store = _store(memory_database, history_turns=1, memory_enabled=True, clock=lambda: now[0])
    store.commit_exchange("s", "old-1", "a1")
    old = store.commit_exchange("s", "old-2", "a2")
    if recreate == "clear":
        store.clear("s")
    else:
        now[0] = datetime(2026, 9, 15, 2, tzinfo=UTC)
        if recreate == "cleanup":
            store.cleanup_expired()
        else:
            assert store.context("s").messages == ()
    store = _store(memory_database, history_turns=1, memory_enabled=True, clock=lambda: now[0])
    store.commit_exchange("s", "new-1", "a3")
    current = store.commit_exchange("s", "new-2", "a4")
    assert old is not None and current is not None
    assert old.revision == current.revision == 1
    assert store.apply_memory(old, "不得恢复的旧会话") is False
    assert store.apply_memory(current, "新会话摘要") is True
    assert store.context("s").memory == "新会话摘要"


@pytest.mark.parametrize("recreate", ["clear", "expire", "evict"])
def test_old_compression_cannot_overwrite_recreated_in_memory_session(recreate: str) -> None:
    """重置、过期或 LRU 淘汰都开启新会话代际，拒绝旧压缩结果。"""
    now = [0.0]
    store = ConversationStore(
        history_turns=1,
        max_sessions=1,
        ttl_seconds=60,
        memory_enabled=True,
        clock=lambda: now[0],
    )
    store.commit_exchange("s", "old-1", "a1")
    old = store.commit_exchange("s", "old-2", "a2")
    if recreate == "clear":
        store.clear("s")
    elif recreate == "expire":
        now[0] = 61.0
    else:
        store.commit_exchange("other", "u", "a")
    store.commit_exchange("s", "new-1", "a3")
    current = store.commit_exchange("s", "new-2", "a4")
    assert old is not None and current is not None
    assert old.revision == current.revision == 1
    assert store.apply_memory(old, "不得恢复的旧会话") is False
    assert store.apply_memory(current, "新会话摘要") is True
    assert store.context("s").memory == "新会话摘要"


def test_compression_reads_all_evicted_turns_across_fixed_batches(memory_database) -> None:
    store = _store(
        memory_database,
        history_turns=300,
        memory_enabled=True,
        max_history_chars=100_000,
    )
    for index in range(300):
        assert store.commit_exchange("qq_session", f"u{index}", f"a{index}") is None

    compression = store.commit_exchange("qq_session", "u300", "a300")

    assert compression is not None
    assert len(compression.turns) == 151
    assert compression.turns[0].user == "u0"
    assert compression.turns[-1].user == "u150"


def test_conversation_expiry_explicitly_deletes_turns_without_foreign_key(
    memory_database,
) -> None:
    now = [datetime(2026, 9, 15, tzinfo=UTC)]
    store = _store(memory_database, clock=lambda: now[0])
    store.commit_exchange("qq_session", "u", "a")

    now[0] = datetime(2026, 9, 15, 0, 59, 59, tzinfo=UTC)
    assert store.context("qq_session").messages
    now[0] = datetime(2026, 9, 15, 1, 59, 58, tzinfo=UTC)
    assert store.context("qq_session").messages
    now[0] = datetime(2026, 9, 15, 2, 59, 58, tzinfo=UTC)
    assert store.context("qq_session").messages == ()

    connection = memory_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM chat_memory_turn WHERE session_id = %s",
                ("qq_session",),
            )
            assert cursor.fetchall() == []
    finally:
        connection.close()


def test_conversation_clear_explicitly_deletes_turns_without_foreign_key(
    memory_database,
) -> None:
    store = _store(memory_database)
    store.commit_exchange("qq_session", "u", "a")

    assert store.clear("qq_session") is True
    assert store.clear("qq_session") is False
    assert store.context("qq_session").messages == ()

    connection = memory_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM chat_memory_turn WHERE session_id = %s",
                ("qq_session",),
            )
            assert cursor.fetchall() == []
    finally:
        connection.close()


def test_expired_cleanup_explicitly_deletes_turns_without_foreign_key(
    memory_database,
) -> None:
    now = [datetime(2026, 9, 15, tzinfo=UTC)]
    store = _store(memory_database, clock=lambda: now[0])
    store.commit_exchange("qq_session", "u", "a")
    now[0] = datetime(2026, 9, 15, 1, 0, 1, tzinfo=UTC)

    assert store.cleanup_expired() == 1

    connection = memory_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM chat_memory_turn WHERE session_id = %s",
                ("qq_session",),
            )
            assert cursor.fetchall() == []
    finally:
        connection.close()


def test_concurrent_conversation_commits_do_not_drop_turns(memory_database) -> None:
    store = _store(memory_database, history_turns=10)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(store.commit_exchange, "qq_session", "u1", "a1"),
            executor.submit(store.commit_exchange, "qq_session", "u2", "a2"),
        ]
        for future in futures:
            future.result()

    contents = [message.content for message in store.context("qq_session").messages]
    assert contents in (["u1", "a1", "u2", "a2"], ["u2", "a2", "u1", "a1"])


def test_database_error_is_mapped_without_connection_details() -> None:
    def fail_connect(**kwargs: object) -> Any:
        raise OperationalError(2003, "private-database-address")

    store = _store(fail_connect)

    with pytest.raises(RuntimeError) as exc_info:
        store.context("qq_session")
    assert str(exc_info.value) == "failed to load conversation memory"
    assert "private-database-address" not in str(exc_info.value)


def _group_store(
    connect: Callable[..., Any],
    *,
    clock: Callable[[], datetime] | None = None,
) -> MySqlRecentGroupContextStore:
    return MySqlRecentGroupContextStore(
        DatabaseParams(
            host="127.0.0.1",
            port=3306,
            user="test",
            password="test",
            database="test",
        ),
        secret="test-session-secret-with-enough-entropy",
        policy=GroupContextPolicy(
            message_limit=2,
            max_chars=1000,
            ttl_seconds=3600,
        ),
        connect=connect,
        clock=clock or (lambda: datetime(2026, 9, 15, tzinfo=UTC)),
    )


def test_group_context_survives_reconstruction_and_hides_scope_ids(
    memory_database,
) -> None:
    first = _group_store(memory_database)
    first.record(
        bot_id="90001",
        group_id="20001",
        sender_id="10001",
        sender_name="小明",
        content="第一条",
    )
    second = _group_store(memory_database)

    assert second.context(bot_id="90001", group_id="20001") == ("小明(QQ 10001)：第一条")

    connection = memory_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT scope_id FROM chat_group_context")
            scope_id = cursor.fetchone()["scope_id"]
    finally:
        connection.close()
    assert "90001" not in scope_id
    assert "20001" not in scope_id


def test_group_context_keeps_latest_messages_in_original_order(memory_database) -> None:
    store = _group_store(memory_database)
    for index in range(1, 4):
        store.record(
            bot_id="90001",
            group_id="20001",
            sender_id=f"1000{index}",
            sender_name=f"成员{index}",
            content=f"第{index}条",
        )

    assert store.context(bot_id="90001", group_id="20001") == (
        "成员2(QQ 10002)：第2条\n成员3(QQ 10003)：第3条"
    )


def test_group_context_rejects_corrupted_json(memory_database) -> None:
    scope_id = external_session_id(
        "bot:90001:group:20001:recent",
        "test-session-secret-with-enough-entropy",
    )
    connection = memory_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO chat_group_context (
                    scope_id, messages_json, expires_at, create_time, update_time
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    scope_id,
                    "not-json",
                    datetime(2026, 9, 16),
                    datetime(2026, 9, 15),
                    datetime(2026, 9, 15),
                ),
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="group context payload is invalid"):
        _group_store(memory_database).context(bot_id="90001", group_id="20001")


def test_group_context_rejects_non_string_sender_id_in_persisted_json(
    memory_database,
) -> None:
    scope_id = external_session_id(
        "bot:90001:group:20001:recent",
        "test-session-secret-with-enough-entropy",
    )
    connection = memory_database()
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO chat_group_context (
                    scope_id, messages_json, expires_at, create_time, update_time
                ) VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    scope_id,
                    '[{"sender_id":10001,"sender_name":"小明","content":"你好"}]',
                    datetime(2026, 9, 16),
                    datetime(2026, 9, 15),
                    datetime(2026, 9, 15),
                ),
            )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="group context payload is invalid"):
        _group_store(memory_database).context(bot_id="90001", group_id="20001")


def test_group_context_read_renews_ttl(memory_database) -> None:
    now = [datetime(2026, 9, 15, tzinfo=UTC)]
    store = _group_store(memory_database, clock=lambda: now[0])
    store.record(
        bot_id="90001",
        group_id="20001",
        sender_id="10001",
        sender_name="小明",
        content="你好",
    )
    now[0] = datetime(2026, 9, 15, 0, 59, 59, tzinfo=UTC)
    assert store.context(bot_id="90001", group_id="20001") is not None
    now[0] = datetime(2026, 9, 15, 1, 59, 58, tzinfo=UTC)
    assert store.context(bot_id="90001", group_id="20001") is not None


def test_committed_history_carries_execution_reference_and_clear_removes_it(memory_database):
    """只有已提交近期轮次能引用 Java 执行上下文；/new 不恢复旧工具。"""
    store = _store(memory_database)
    store.commit_exchange("s", "question", "answer", request_id="qq-job")
    messages = _store(memory_database).context("s").messages
    assert [m.history_request_id for m in messages] == ["qq-job", "qq-job"]
    assert messages[0].model_dump(by_alias=True)["historyRequestId"] == "qq-job"
    store.clear("s")
    assert store.context("s").messages == ()
