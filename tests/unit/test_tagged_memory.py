"""标签记忆的作用域、事务存储及 MCP 上下文契约。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from agent_bot.services.tagged_memory import MemoryArguments, MemoryIdentity


def test_memory_identity_binds_scopes_to_bot_and_current_subject() -> None:
    """只有同一机器人下的全局记忆共享，其他作用域按主体隔离。"""
    first = MemoryIdentity(bot_id="10000", user_id="10001", session_id="a", group_id="20000")
    other = MemoryIdentity(bot_id="10000", user_id="10002", session_id="b", group_id="20000")
    foreign = MemoryIdentity(bot_id="10009", user_id="10001", session_id="a", group_id="20000")
    assert first.owner("global", "secret") == other.owner("global", "secret")
    assert first.owner("group", "secret") == other.owner("group", "secret")
    assert first.owner("user", "secret") != other.owner("user", "secret")
    assert first.owner("session", "secret") != other.owner("session", "secret")
    for scope in first.scopes:
        assert first.owner(scope, "secret") != foreign.owner(scope, "secret")
    private = MemoryIdentity(bot_id="10000", user_id="10001", session_id="c")
    with pytest.raises(ValueError):
        private.owner("group", "secret")


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "save", "key": "游戏", "content": "喜欢永劫", "tags": []},
        {"action": "save", "scope": "all", "key": "游戏", "content": "喜欢永劫", "tags": ["游戏"]},
        {"action": "recall", "tags": []},
        {"action": "recall", "tags": ["a" * 33]},
        {"action": "recall", "tags": ["\n"]},
        {"action": "save", "key": "x", "content": "x" * 1001, "tags": ["游戏"]},
        {"action": "tags", "session_id": "another-session"},
    ],
)
def test_memory_arguments_reject_invalid_or_unbounded_access(payload: dict) -> None:
    """拒绝跨会话标识、无效操作和超限字段。"""
    with pytest.raises(ValidationError):
        MemoryArguments.model_validate(payload)


def test_tags_are_normalized_and_deduplicated() -> None:
    """全半角、大小写和首尾空格不会生成不同标签。"""
    args = MemoryArguments(
        action="recall",
        scope="all",
        tags=[" \uff27\uff21\uff2d\uff25 ", "game", "游戏"],
    )
    assert args.tags == ("game", "游戏")


@pytest.fixture
def memory_store(tmp_path):
    """用 SQLite 执行真实关系查询，只翻译 MySQL 的建表与锁语法。"""
    import re
    import sqlite3
    from datetime import datetime

    from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
    from agent_bot.settings import DatabaseParams

    class Connection:
        """每次租用一个离线数据库事务。"""

        def __init__(self, **_kwargs):
            """所有线程共享临时文件，连接各自关闭。"""
            self.db = sqlite3.connect(tmp_path / "memory.db")
            self.db.row_factory = sqlite3.Row
            self.current = self.db.cursor()

        def cursor(self):
            """兼容字典游标。"""
            return self

        def __enter__(self):
            """进入游标上下文。"""
            return self

        def __exit__(self, *_args):
            """事务由存储对象管理。"""

        def execute(self, sql, args=()):
            """只翻译方言，筛选、排序、关联、事务仍由数据库执行。"""
            sql = sql.replace("%s", "?").replace(
                "ON DUPLICATE KEY UPDATE scope = VALUES(scope)",
                "ON CONFLICT(scope_id) DO UPDATE SET scope = excluded.scope",
            )
            sql = sql.replace(
                "id INT NOT NULL AUTO_INCREMENT", "id INTEGER PRIMARY KEY AUTOINCREMENT"
            )
            sql = re.sub(r",\s*PRIMARY KEY \(id\)", "", sql)
            sql = sql.replace(" FOR UPDATE", "")
            sql = re.sub(r" CHARACTER SET ascii| COLLATE \w+| COMMENT '[^']*'", "", sql)
            sql = re.sub(r" ENGINE=InnoDB.*", "", sql)
            sql = re.sub(r"UNIQUE KEY \w+", "UNIQUE", sql)
            sql = re.sub(r",\s*KEY \w+ \([^)]*\)", "", sql)
            return self.current.execute(
                sql,
                tuple(
                    value.isoformat() if isinstance(value, datetime) else value for value in args
                ),
            )

        def executemany(self, sql, rows):
            """逐条执行真实参数化标签插入。"""
            for row in rows:
                self.execute(sql, row)

        def fetchone(self):
            """返回字典行。"""
            row = self.current.fetchone()
            return dict(row) if row else None

        def fetchall(self):
            """返回字典行列表。"""
            return [dict(row) for row in self.current.fetchall()]

        @property
        def lastrowid(self):
            """与 PyMySQL 游标一样返回刚插入的整数主键。"""
            return self.current.lastrowid

        def commit(self):
            """提交事务。"""
            self.db.commit()

        def rollback(self):
            """回滚事务。"""
            self.db.rollback()

        def close(self):
            """释放连接。"""
            self.db.close()

    store = TaggedMemoryStore(
        DatabaseParams("localhost", 3306, "test", "test", "test"),
        secret="test-secret",
        connect=Connection,
    )
    store.initialize()
    return store


def test_store_scopes_upsert_tag_replacement_and_forget(memory_store) -> None:
    """验证关联查询和更新原子替换标签，遗忘后不残留目录标签。"""
    first = MemoryIdentity(bot_id="10000", user_id="10001", session_id="a", group_id="20000")
    other = MemoryIdentity(bot_id="10000", user_id="10002", session_id="b", group_id="20000")
    private = MemoryIdentity(bot_id="10000", user_id="10001", session_id="c")
    foreign = MemoryIdentity(bot_id="10009", user_id="10001", session_id="a", group_id="20000")
    read = MemoryArguments(action="recall", scope="all", tags=["游戏"])
    for scope in first.scopes:
        memory_store.execute(
            first,
            MemoryArguments(
                action="save",
                scope=scope,
                key="喜欢的游戏",
                content=scope,
                tags=["游戏"],
            ),
        )
    assert {record.scope for record in memory_store.execute(other, read).records} == {
        "global",
        "group",
    }
    assert {record.scope for record in memory_store.execute(private, read).records} == {
        "global",
        "user",
    }
    assert memory_store.execute(foreign, read).records == ()
    save = MemoryArguments(action="save", key="喜欢的游戏", content="永劫", tags=["永劫"])
    memory_store.execute(first, save)
    memory_store.execute(first, save)
    records = memory_store.execute(first, MemoryArguments(action="recall", tags=["永劫"])).records
    assert len(records) == 1
    assert records[0].content == "永劫"
    old_tag = memory_store.execute(first, MemoryArguments(action="recall", tags=["游戏"]))
    assert old_tag.records == ()
    forget = MemoryArguments(action="forget", key="喜欢的游戏")
    assert memory_store.execute(first, forget).status == "forgotten"
    assert memory_store.execute(first, forget).status == "not_found"
    assert memory_store.execute(first, MemoryArguments(action="tags")).tags == ()


def test_memory_ids_are_incrementing_integers_and_tag_links_use_them(
    memory_store, tmp_path,
) -> None:
    """新记忆由数据库分配整数 ID，标签关联使用同一个整数。"""
    import sqlite3

    identity = MemoryIdentity(bot_id="bot", user_id="user", session_id="session")
    for key in ("first", "second"):
        memory_store.execute(
            identity,
            MemoryArguments(action="save", key=key, content=key, tags=["topic"]),
        )
    with sqlite3.connect(tmp_path / "memory.db") as connection:
        ids = [row[0] for row in connection.execute(
            "SELECT id FROM dispatch_chat_tagged_memory ORDER BY id"
        )]
        tag_ids = [row[0] for row in connection.execute(
            "SELECT memory_id FROM dispatch_chat_memory_tag ORDER BY memory_id"
        )]
    assert ids == [1, 2]
    assert tag_ids == ids


def test_forget_hides_memory_without_deleting_row_and_save_restores_it(
    memory_store, tmp_path,
) -> None:
    """遗忘只标记删除，后续同 key 保存恢复原 ID 并替换标签。"""
    import sqlite3

    identity = MemoryIdentity(bot_id="bot", user_id="user", session_id="session")
    memory_store.execute(
        identity,
        MemoryArguments(action="save", key="preference", content="old", tags=["old"]),
    )
    with sqlite3.connect(tmp_path / "memory.db") as connection:
        original_id = connection.execute(
            "SELECT id FROM dispatch_chat_tagged_memory WHERE memory_key = 'preference'"
        ).fetchone()[0]
    forget = MemoryArguments(action="forget", key="preference")
    assert memory_store.execute(identity, forget).status == "forgotten"
    assert memory_store.execute(identity, forget).status == "not_found"
    assert memory_store.execute(identity, MemoryArguments(action="tags")).tags == ()
    assert memory_store.execute(
        identity, MemoryArguments(action="recall", tags=["old"])
    ).records == ()
    with sqlite3.connect(tmp_path / "memory.db") as connection:
        row = connection.execute(
            "SELECT id, content FROM dispatch_chat_tagged_memory WHERE memory_key = 'preference'"
        ).fetchone()
        assert row is not None
        deleted = connection.execute(
            "SELECT is_deleted FROM dispatch_chat_tagged_memory WHERE id = ?", (original_id,)
        ).fetchone()[0]
        old_tags = connection.execute(
            "SELECT tag FROM dispatch_chat_memory_tag WHERE memory_id = ?", (original_id,)
        ).fetchall()
    assert row == (original_id, "old")
    assert deleted == 1
    assert old_tags == [("old",)]

    memory_store.execute(
        identity,
        MemoryArguments(action="save", key="preference", content="new", tags=["new"]),
    )
    with sqlite3.connect(tmp_path / "memory.db") as connection:
        restored = connection.execute(
            "SELECT id, is_deleted FROM dispatch_chat_tagged_memory WHERE memory_key = 'preference'"
        ).fetchone()
    assert restored == (original_id, 0)
    assert memory_store.execute(
        identity, MemoryArguments(action="recall", tags=["old"])
    ).records == ()
    assert memory_store.execute(
        identity, MemoryArguments(action="recall", tags=["new"])
    ).records[0].content == "new"


def test_memory_queries_are_parameterized_and_results_bounded(memory_store) -> None:
    """带 SQL 字符的内容按数据存储，召回上限与目录分页不会截断正文。"""
    identity = MemoryIdentity(bot_id="bot", user_id="user", session_id="session")
    tag = "话题' OR 1=1 --"
    for index in range(70):
        memory_store.execute(
            identity,
            MemoryArguments(
                action="save",
                key=f"key-{index}",
                content="原文' --",
                tags=[tag, f"topic-{index}"],
            ),
        )
    result = memory_store.execute(identity, MemoryArguments(action="recall", tags=[tag]))
    assert len(result.records) == 8
    assert all(record.content == "原文' --" for record in result.records)
    first = memory_store.execute(identity, MemoryArguments(action="tags"))
    second = memory_store.execute(identity, MemoryArguments(action="tags", offset=64))
    assert len(first.tags) == 64 and first.next_offset == 64
    assert len(second.tags) == 7 and second.next_offset is None
    assert not {item.tag for item in first.tags}.intersection(item.tag for item in second.tags)


def test_memory_write_rolls_back_before_expired_request_commits(memory_store, monkeypatch) -> None:
    """截止时间在写入过程中到达时，正文和标签一起回滚。"""
    from unittest.mock import Mock

    from agent_bot.integrations.tagged_memory_store import TaggedMemoryStoreError

    identity = MemoryIdentity(bot_id="bot", user_id="user", session_id="session")
    with monkeypatch.context() as patcher:
        patcher.setattr(
            memory_store,
            "_check_deadline",
            Mock(
                side_effect=[None, TaggedMemoryStoreError("expired")],
            ),
        )
        with pytest.raises(TaggedMemoryStoreError):
            memory_store.execute(
                identity,
                MemoryArguments(
                    action="save",
                    key="key",
                    content="fact",
                    tags=["tag"],
                ),
            )
    assert memory_store.execute(identity, MemoryArguments(action="tags")).tags == ()


@pytest.mark.asyncio
async def test_mcp_memory_catalog_and_recall_use_the_token_identity(memory_store) -> None:
    """跨请求保存再读取，目录只公开标签，召回结果通过 MCP 加入模型上下文。"""
    import json

    from agent_bot.integrations.mcp import ChatMcpServer
    from agent_bot.services.chat_tools import ChatToolContext, build_chat_tool_registry
    from agent_bot.services.tagged_memory import MemorySession
    from agent_bot.services.tool_sessions import ToolSessionStore

    identity = MemoryIdentity(bot_id="bot", user_id="user", session_id="session")
    sessions = ToolSessionStore(ttl_seconds=30, max_sessions=3)
    server = ChatMcpServer(
        store=sessions,
        registry=build_chat_tool_registry(),
        allowed_hosts=["localhost:*"],
    )
    token = await sessions.create(
        ChatToolContext(
            session_id="session",
            user_id="user",
            allowed_tools=frozenset({"memory"}),
            memory_session=MemorySession(memory_store, identity),
        )
    )
    save = await server.call_tool(
        token,
        "memory",
        {
            "action": "save",
            "scope": "user",
            "key": "口味",
            "content": "爱吃甜点",
            "tags": ["饮食"],
        },
    )
    assert not save.is_error
    catalog = await server.list_tools(token)
    assert "饮食" in catalog.tools[0].description
    assert "爱吃甜点" not in catalog.tools[0].description
    recalled = await server.call_tool(
        token,
        "memory",
        {
            "action": "recall",
            "scope": "all",
            "tags": ["饮食"],
        },
    )
    assert json.loads(recalled.content[0].text)["records"][0]["content"] == "爱吃甜点"
    forbidden = await server.call_tool(
        token,
        "memory",
        {
            "action": "recall",
            "tags": ["饮食"],
            "user_id": "another-user",
        },
    )
    assert forbidden.is_error
    for _ in range(3):
        assert not (
            await server.call_tool(
                token,
                "memory",
                {
                    "action": "save",
                    "key": "key",
                    "content": "fact",
                    "tags": ["topic"],
                },
            )
        ).is_error
    assert (
        await server.call_tool(
            token,
            "memory",
            {
                "action": "save",
                "key": "new-key",
                "content": "fact",
                "tags": ["topic"],
            },
        )
    ).is_error


def test_plain_chat_selects_authorized_memory_without_keywords() -> None:
    """普通对话也声明记忆能力，未授权请求不会获得工具。"""
    from agent_bot.services.chat.routing import ChatRouteSelector

    router = ChatRouteSelector()
    assert router.select("我更喜欢甜的", available_tools=("memory",)).chat_tools == ("memory",)
    assert router.select("记住我的喜好").chat_tools == ()


def test_scope_capacity_allows_updates_but_rejects_new_records(memory_store) -> None:
    """容量达到硬上限后仍能更正已有记忆，不能无限写入新键。"""
    identity = MemoryIdentity(bot_id="bot", user_id="user", session_id="session")
    for index in range(256):
        memory_store.execute(
            identity,
            MemoryArguments(
                action="save",
                key=str(index),
                content="fact",
                tags=["topic"],
            ),
        )
    with pytest.raises(ValueError, match="capacity"):
        memory_store.execute(
            identity,
            MemoryArguments(
                action="save",
                key="257",
                content="fact",
                tags=["new"],
            ),
        )
    memory_store.execute(
        identity,
        MemoryArguments(
            action="save",
            key="0",
            content="changed",
            tags=["changed"],
        ),
    )
    result = memory_store.execute(identity, MemoryArguments(action="recall", tags=["changed"]))
    assert result.records[0].content == "changed"
    assert memory_store.execute(identity, MemoryArguments(action="forget", key="0")).status == (
        "forgotten"
    )
    memory_store.execute(
        identity,
        MemoryArguments(action="save", key="257", content="replacement", tags=["new"]),
    )
    with pytest.raises(ValueError, match="capacity"):
        memory_store.execute(
            identity,
            MemoryArguments(action="save", key="0", content="restored", tags=["changed"]),
        )


def test_schema_script_matches_automatic_initialization() -> None:
    """手工升级和服务启动使用同一张表结构。"""
    from pathlib import Path

    from agent_bot.integrations.tagged_memory_store import SCHEMA_STATEMENTS

    script = (Path(__file__).parents[2] / "sql/legacy/2026-09-22-tagged-memory.sql").read_text(
        encoding="utf-8",
    )
    normalized = " ".join(script.split())
    for statement in SCHEMA_STATEMENTS:
        assert " ".join(statement.split()) + ";" in normalized


@pytest.mark.asyncio
async def test_database_errors_are_sanitized_in_memory_tool() -> None:
    """数据库供应商信息不能出现在 MCP 错误结果中。"""
    from unittest.mock import Mock

    from pymysql.err import OperationalError

    from agent_bot.integrations.tagged_memory_store import TaggedMemoryStore
    from agent_bot.services.chat_tools import (
        ChatToolContext,
        ChatToolError,
        build_chat_tool_registry,
    )
    from agent_bot.services.tagged_memory import MemorySession
    from agent_bot.settings import DatabaseParams

    store = TaggedMemoryStore(
        DatabaseParams("localhost", 3306, "test", "test", "test"),
        secret="test-secret",
        connect=Mock(side_effect=OperationalError(2003, "private-db-token")),
    )
    context = ChatToolContext(
        session_id="session",
        user_id="user",
        allowed_tools=frozenset({"memory"}),
        memory_session=MemorySession(
            store,
            MemoryIdentity(bot_id="bot", user_id="user", session_id="session"),
        ),
    )
    with pytest.raises(ChatToolError) as caught:
        await build_chat_tool_registry().call("memory", {"action": "tags"}, context)
    assert "private-db-token" not in str(caught.value)
