"""隔离 SQLite 验证知识筛选、分页、逻辑删除和增量数据保留。"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

import pymysql
import pytest

from agent_bot.database import database_connector, initialize_database
from agent_bot.knowledge.contracts import KnowledgeCreate, KnowledgeSearchArguments
from agent_bot.knowledge.errors import KnowledgeError
from agent_bot.knowledge.store import KnowledgeStore
from agent_bot.settings import DatabaseParams


@pytest.fixture
def knowledge_database(tmp_path):
    """生产初始化脚本只作用于测试目录。"""
    params = DatabaseParams(backend="sqlite", sqlite_path=str(tmp_path / "knowledge.db"))
    initialize_database(params)
    return params


@pytest.fixture
def knowledge_store(knowledge_database):
    """真实生产适配器执行参数化查询。"""
    return KnowledgeStore(knowledge_database, connect=database_connector(knowledge_database))


async def test_knowledge_create_filter_delete(knowledge_store, knowledge_database):
    """过滤先于分页，删除后正文保留而模型和标签目录不可见。"""
    first = await knowledge_store.create(
        KnowledgeCreate(title="安装步骤", content="中文安装说明", tags=["部署"])
    )
    second = await knowledge_store.create(
        KnowledgeCreate(title="启动说明", content="中文启动方法", tags=["部署", "启动"])
    )
    page = await knowledge_store.list(limit=1, query="中文", tag="部署")
    assert [item.id for item in page.items] == [second.id]
    next_page = await knowledge_store.list(
        limit=1, query="中文", tag="部署", cursor=page.next_cursor
    )
    assert [item.id for item in next_page.items] == [first.id]
    with pytest.raises(KnowledgeError):
        await knowledge_store.list(cursor=page.next_cursor, query="无关")
    assert {tag.tag: tag.count for tag in await knowledge_store.tags()} == {"部署": 2, "启动": 1}
    await knowledge_store.delete(second.id)
    assert [item.id for item in (await knowledge_store.list()).items] == [first.id]
    with pytest.raises(KnowledgeError) as failure:
        await knowledge_store.get(second.id)
    assert failure.value.status_code == 404
    with sqlite3.connect(knowledge_database.sqlite_path) as db:
        assert db.execute(
            "SELECT content,is_deleted FROM chat_knowledge WHERE id=?", (second.id,)
        ).fetchone() == ("中文启动方法", 1)
    assert {tag.tag: tag.count for tag in await knowledge_store.tags()} == {"部署": 1}


async def test_search_literal_keywords_and_truncation(knowledge_store):
    """模型不会把用户通配符当 SQL，正文和结果数受固定边界约束。"""
    for index in range(7):
        await knowledge_store.create(
            KnowledgeCreate(
                title=f"步骤 {index}", content="含 %_! 字符" + "文" * 2200, tags=["部署"]
            )
        )
    await knowledge_store.create(KnowledgeCreate(title="无关", content="普通内容", tags=["部署"]))
    results = await knowledge_store.search(KnowledgeSearchArguments(query="%_!", tags=["部署"]))
    assert len(results) == 5
    assert all(len(item.content) == 2000 and item.truncated for item in results)
    assert [item.id for item in results] == [7, 6, 5, 4, 3]
    assert await knowledge_store.search(KnowledgeSearchArguments(query="不存在")) == ()


def test_incremental_schema_matches_initialization_and_preserves_data(knowledge_database, tmp_path):
    """增量只补两张表，旧记忆不会被删除，新旧知识结构一致。"""
    root = Path(__file__).resolve().parents[2]
    initial = (root / "sql/agent_bot_init.sqlite.sql").read_text(encoding="utf-8")
    migration = (root / "sql/2026-10-14-tags-knowledge-memory.sqlite.sql").read_text(
        encoding="utf-8"
    )
    with (
        sqlite3.connect(tmp_path / "old.db") as old,
        sqlite3.connect(knowledge_database.sqlite_path) as fresh,
    ):
        # 用当前完整基线移除知识表，模拟已有业务库，而非只测同一段 DDL 文本。
        old.executescript(initial)
        old.execute("DROP TABLE chat_knowledge_tag")
        old.execute("DROP TABLE chat_knowledge")
        old.execute("INSERT INTO chat_memory_scope VALUES (?,?)", ("a" * 64, "user"))
        old.executescript(migration)
        for table in ("chat_knowledge", "chat_knowledge_tag"):
            assert (
                old.execute(f"PRAGMA table_info({table})").fetchall()
                == fresh.execute(f"PRAGMA table_info({table})").fetchall()
            )
            assert (
                old.execute(f"PRAGMA index_list({table})").fetchall()
                == fresh.execute(f"PRAGMA index_list({table})").fetchall()
            )
        assert old.execute("SELECT scope_id FROM chat_memory_scope").fetchone() == ("a" * 64,)


async def test_tag_write_failure_rolls_back_entry(knowledge_database):
    """模拟 MySQL 驱动标签写入失败，真实事务不留下没有标签的正文。"""
    connect = database_connector(knowledge_database)

    class FailingTagsConnection:
        """只替换发生错误的驱动边界，其余读写交给生产适配器。"""

        def __init__(self, **kwargs):
            """每次事务建立真实隔离连接。"""
            self.connection = connect(**kwargs)

        def __getattr__(self, name):
            """连接提交、回滚和关闭仍真实执行。"""
            return getattr(self.connection, name)

        @contextmanager
        def cursor(self):
            """游标仅在插入标签时注入驱动错误。"""
            with self.connection.cursor() as cursor:
                original = cursor.executemany

                def fail_tags(sql, rows):
                    """数据库拒绝标签写入，错误细节不能返回给用户。"""
                    if "chat_knowledge_tag" in sql:
                        raise pymysql.OperationalError(1105, "private database error")
                    return original(sql, rows)

                cursor.executemany = fail_tags
                yield cursor

    store = KnowledgeStore(knowledge_database, connect=FailingTagsConnection)
    with pytest.raises(KnowledgeError) as failure:
        await store.create(KnowledgeCreate(title="失败条目", content="应回滚", tags=["主题"]))
    assert failure.value.code == "KNOWLEDGE_UNAVAILABLE"
    with sqlite3.connect(knowledge_database.sqlite_path) as db:
        assert db.execute("SELECT COUNT(*) FROM chat_knowledge").fetchone() == (0,)
