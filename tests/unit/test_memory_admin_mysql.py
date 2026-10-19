"""模拟 MySQL 一致性快照，验证管理员新增先加锁再做容量和同键判断。"""

from datetime import datetime

import pymysql
import pytest

from agent_bot.integrations.tagged_memory_store import (
    TaggedMemoryCapacityError,
    TaggedMemoryConflictError,
    TaggedMemoryStore,
)
from agent_bot.knowledge.contracts import MemoryCreate
from agent_bot.settings import DatabaseParams


class RepeatableReadConnection:
    """另一事务在当前线程等锁期间提交，普通读保留先前一致性快照。"""

    def __init__(self, mode: str):
        """容量和同键两种并发提交共用实际事务顺序模型。"""
        self.mode = mode
        self.snapshot = False
        self.result = None
        self.lastrowid = 257
        self.inserted = False
        self.events = []

    def cursor(self):
        """事务使用一个受控字典游标。"""
        return self

    def __enter__(self):
        """模拟游标上下文，连接所有权仍属于生产存储。"""
        return self

    def __exit__(self, *_args):
        """退出游标不改变数据库快照。"""

    def execute(self, sql, args=()):
        """只有首次非锁定读取建立旧快照，当前读始终看到并发提交。"""
        self.events.append(sql)
        if sql.startswith("SELECT GET_LOCK("):
            self.snapshot = False
            self.result = {"acquired": 1}
        elif sql.startswith("SELECT RELEASE_LOCK("):
            self.result = {"released": 1}
        elif sql.startswith("SELECT scope FROM"):
            self.result = {"scope": "user"}
        elif sql.startswith("SELECT id, is_deleted"):
            self.result = (
                {"id": 256, "is_deleted": 0}
                if self.mode == "duplicate" and not self.snapshot
                else None
            )
        elif sql.startswith("SELECT COUNT(*)"):
            self.result = {"count": 255 if self.snapshot else 256}
        elif sql.startswith("INSERT INTO chat_tagged_memory"):
            if self.mode == "duplicate":
                raise pymysql.IntegrityError(1062, "duplicate scope key")
            self.inserted = True
        elif sql.startswith("SELECT id FROM"):
            self.result = {"id": 257}
        elif sql.startswith("SELECT m.*"):
            self.result = {
                "id": 257,
                "scope_id": "a" * 64,
                "scope": "user",
                "memory_key": "新键",
                "content": "事实",
                "create_time": datetime.now(),
            }
        elif sql.startswith("SELECT memory_id,tag"):
            self.result = [{"memory_id": 257, "tag": "主题"}]

    def executemany(self, *_args):
        """容量不通过时不应到达标签插入。"""

    def fetchone(self):
        """返回本次执行的字典结果。"""
        return self.result

    def fetchall(self):
        """返回完整标签集合。"""
        return self.result

    def commit(self):
        """测试数据库没有外部提交副作用。"""

    def rollback(self):
        """错误清除当前事务中的插入状态。"""
        self.inserted = False

    def close(self):
        """连接由存储在退出时释放。"""


@pytest.mark.parametrize(
    "mode,error",
    [("capacity", TaggedMemoryCapacityError), ("duplicate", TaggedMemoryConflictError)],
)
def test_existing_scope_observes_concurrent_commit_before_admission(mode, error):
    """已有作用域的首个读若未锁定，会用旧快照越过容量或误报不可用。"""
    connection = RepeatableReadConnection(mode)
    store = TaggedMemoryStore(DatabaseParams(), secret="offline", connect=lambda **_: connection)
    with pytest.raises(error):
        store.admin_create(
            MemoryCreate(scope="user", scope_id="a" * 64, key="新键", content="事实", tags=["主题"])
        )
    assert not connection.inserted
    assert connection.events[0].startswith("SELECT GET_LOCK(")
    assert connection.events[-1].startswith("SELECT RELEASE_LOCK(")
