from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from pymysql.err import DatabaseError

from agent_bot.group_members import (
    GroupMemberStore,
    GroupMemberStoreError,
    GroupMemberValidationError,
)
from agent_bot.settings import DatabaseParams


@dataclass
class FakeDatabase:
    select_rows: list[dict[str, object]] = field(default_factory=list)
    fail_batch: bool = False
    connections: list[FakeConnection] = field(default_factory=list)

    def connect(self, **_kwargs: object) -> FakeConnection:
        connection = FakeConnection(self)
        self.connections.append(connection)
        return connection


class FakeCursor:
    def __init__(self, database: FakeDatabase) -> None:
        self.database = database
        self.executions: list[tuple[str, object]] = []
        self.batches: list[tuple[str, list[tuple[object, ...]]]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, parameters: object = None) -> int:
        self.executions.append((sql, parameters))
        return len(self.database.select_rows)

    def executemany(self, sql: str, rows: list[tuple[object, ...]]) -> int:
        self.batches.append((sql, rows))
        if self.database.fail_batch:
            raise DatabaseError("sensitive database detail")
        return len(rows)

    def fetchall(self) -> list[dict[str, object]]:
        return [dict(row) for row in self.database.select_rows]


class FakeConnection:
    def __init__(self, database: FakeDatabase) -> None:
        self.cursor_value = FakeCursor(database)
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def cursor(self) -> FakeCursor:
        return self.cursor_value

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def database_params() -> DatabaseParams:
    return DatabaseParams(
        host="127.0.0.1",
        port=3306,
        user="dispatch_chat",
        password="password",
        database="dispatch_chat",
    )


@pytest.mark.asyncio
async def test_replace_group_validates_then_atomically_writes_normalized_snapshot() -> None:
    database = FakeDatabase()
    store = GroupMemberStore(database_params(), connect=database.connect)

    members = await store.replace_group(
        bot_id="90001",
        group_id="20001",
        group_name="  \uff34\uff45\uff53\uff54 群  ",
        members=[
            {
                "user_id": 10086,
                "nickname": "  小明 ",
                "card": "技术部\n小明",
                "role": "admin",
                "join_time": 1_700_000_000,
                "last_sent_time": 1_700_000_100,
            }
        ],
    )

    connection = database.connections[0]
    delete_sql, delete_parameters = connection.cursor_value.executions[0]
    insert_sql, rows = connection.cursor_value.batches[0]
    assert "DELETE FROM `dispatch_chat_group_member`" in delete_sql
    assert delete_parameters == ("90001", "20001")
    assert "INSERT INTO `dispatch_chat_group_member`" in insert_sql
    assert rows == [
        (
            "90001",
            "20001",
            "10086",
            "Test 群",
            "小明",
            "技术部 小明",
            "admin",
            1_700_000_000,
            1_700_000_100,
        )
    ]
    assert members == [
        {"user_id": "10086", "nickname": "小明", "card": "技术部 小明"}
    ]
    assert connection.commits == 1
    assert connection.rollbacks == 0
    assert connection.closed


@pytest.mark.asyncio
async def test_list_members_returns_only_bounded_mention_fields() -> None:
    database = FakeDatabase(
        select_rows=[
            {
                "user_id": "10086",
                "nickname": "小明",
                "card": "技术部小明",
                "ignored": "must not escape",
            }
        ]
    )
    store = GroupMemberStore(database_params(), connect=database.connect)

    members = await store.list_members("90001", "20001")

    assert members == [
        {"user_id": "10086", "nickname": "小明", "card": "技术部小明"}
    ]
    query, parameters = database.connections[0].cursor_value.executions[0]
    assert "ORDER BY user_id" in query
    assert parameters == ("90001", "20001")


@pytest.mark.asyncio
async def test_replace_group_rolls_back_and_wraps_database_errors() -> None:
    database = FakeDatabase(fail_batch=True)
    store = GroupMemberStore(database_params(), connect=database.connect)

    with pytest.raises(GroupMemberStoreError, match="group member snapshot"):
        await store.replace_group(
            bot_id="90001",
            group_id="20001",
            group_name="测试群",
            members=[{"user_id": 10086, "nickname": "小明", "card": ""}],
        )

    connection = database.connections[0]
    assert connection.commits == 0
    assert connection.rollbacks == 1
    assert connection.closed


@pytest.mark.asyncio
async def test_replace_group_rejects_oversized_snapshot_before_connecting() -> None:
    database = FakeDatabase()
    store = GroupMemberStore(database_params(), connect=database.connect)

    with pytest.raises(GroupMemberValidationError, match="2000"):
        await store.replace_group(
            bot_id="90001",
            group_id="20001",
            group_name="测试群",
            members=[
                {"user_id": 10000 + index, "nickname": str(index), "card": ""}
                for index in range(2001)
            ],
        )

    assert database.connections == []


@pytest.mark.asyncio
async def test_upsert_and_delete_member_use_scoped_parameterized_queries() -> None:
    database = FakeDatabase()
    store = GroupMemberStore(database_params(), connect=database.connect)

    await store.upsert_member(
        bot_id="90001",
        group_id="20001",
        group_name="测试群",
        member={"user_id": 10086, "nickname": "小明", "card": ""},
    )
    await store.delete_member("90001", "20001", "10086")

    upsert_sql, upsert_parameters = database.connections[0].cursor_value.executions[0]
    delete_sql, delete_parameters = database.connections[1].cursor_value.executions[0]
    assert "ON DUPLICATE KEY UPDATE" in upsert_sql
    assert upsert_parameters[:3] == ("90001", "20001", "10086")
    assert "DELETE FROM `dispatch_chat_group_member`" in delete_sql
    assert delete_parameters == ("90001", "20001", "10086")
