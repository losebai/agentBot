"""记忆增量结构定义；普通启动只校验，不能隐式升级已有数据库。"""

import re

MEMORY_COLUMNS = {
    "revision": ("INT NOT NULL DEFAULT 1", "事实乐观锁版本"),
    "topic": ("VARCHAR(80) NOT NULL DEFAULT ''", "用于整理的规范主题"),
    "kind": ("VARCHAR(20) NOT NULL DEFAULT 'fact'", "事实类型"),
    "status": ("VARCHAR(16) NOT NULL DEFAULT 'active'", "生命周期状态"),
    "subject_type": ("VARCHAR(16) NOT NULL DEFAULT 'unknown'", "被描述主体类型"),
    "subject_id": ("VARCHAR(64) NOT NULL DEFAULT ''", "匿名主体标识"),
    "source_kind": ("VARCHAR(24) NOT NULL DEFAULT 'legacy_unknown'", "最近确认的来源类型"),
    "importance": ("DOUBLE NOT NULL DEFAULT 0.5", "重要性排序权重非正确概率"),
    "observed_at": ("DATETIME NULL", "已知事实时间未知为空"),
    "valid_from": ("DATETIME NULL", "UTC有效区间起点含边界"),
    "valid_until": ("DATETIME NULL", "UTC有效区间终点不含边界"),
    "task_state": ("VARCHAR(16) NULL", "任务未完成完成或取消"),
    "merged_into_id": ("INT NULL", "合并后的当前事实标识"),
    "conflict_set_id": ("VARCHAR(64) NULL", "待解决冲突集合"),
    "content_fingerprint": ("CHAR(64) NOT NULL DEFAULT ''", "规范正文摘要用于精确去重候选"),
    "last_used_at": ("DATETIME NULL", "实际采用时间不是事实时间"),
    "use_count": ("INT NOT NULL DEFAULT 0", "幂等累计采用次数"),
    "created_time_basis": ("VARCHAR(24) NOT NULL DEFAULT 'utc'", "创建时间的UTC或旧未知时区依据"),
    "updated_time_basis": ("VARCHAR(24) NOT NULL DEFAULT 'utc'", "更新时间的UTC或旧未知时区依据"),
}

MYSQL_ADDITIONAL = (
    """CREATE TABLE IF NOT EXISTS chat_memory_revision (
      id INT NOT NULL AUTO_INCREMENT COMMENT '历史版本标识',
      memory_id INT NOT NULL COMMENT '当前事实标识',
      revision INT NOT NULL COMMENT '不可变事实版本',
      operation VARCHAR(20) NOT NULL COMMENT '变更操作',
      snapshot_json JSON NOT NULL COMMENT '完整事实与来源快照',
      actor_type VARCHAR(20) NOT NULL COMMENT '管理员用户或迁移来源',
      reason VARCHAR(300) NOT NULL COMMENT '变更理由不含内部推理',
      created_at DATETIME NOT NULL COMMENT 'UTC版本提交时间',
      -- 主键稳定标识历史记录，联合唯一约束禁止覆盖同一事实版本。
      PRIMARY KEY (id),
      UNIQUE KEY uk_memory_revision (memory_id,revision),
      -- 外键防止历史版本脱离当前事实根记录。
      FOREIGN KEY (memory_id) REFERENCES chat_tagged_memory(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='不可变长期记忆版本'""",
    """CREATE TABLE IF NOT EXISTS chat_memory_evidence (
      id INT NOT NULL AUTO_INCREMENT COMMENT '来源证据标识',
      memory_id INT NOT NULL COMMENT '归属事实',
      first_revision INT NOT NULL COMMENT '首次纳入版本',
      source_kind VARCHAR(24) NOT NULL COMMENT '来源类别不由模型指定',
      source_ref VARCHAR(200) NULL COMMENT '可核对来源引用',
      source_actor_id VARCHAR(64) NULL COMMENT '匿名发言人',
      source_session_id VARCHAR(128) NULL COMMENT '来源会话权限边界',
      source_event_id VARCHAR(200) NULL COMMENT '真实来源事件引用',
      source_time DATETIME NULL COMMENT '来源UTC时间未知为空',
      recorded_at DATETIME NOT NULL COMMENT '证据记录UTC时间',
      excerpt VARCHAR(300) NOT NULL COMMENT '真实片段不是可执行指令',
      evidence_fingerprint CHAR(64) NOT NULL COMMENT '独立来源去重摘要',
      original_memory_id INT NULL COMMENT '合并前原始事实',
      -- 主键稳定标识证据，联合唯一约束防止同一来源反复佐证。
      PRIMARY KEY (id),
      UNIQUE KEY uk_memory_evidence (memory_id,evidence_fingerprint),
      -- 来源撤回按事件和会话定向定位，避免锁定无关事实证据。
      KEY idx_memory_evidence_event (source_event_id,source_session_id),
      -- 外键防止来源证据脱离当前事实根记录。
      FOREIGN KEY (memory_id) REFERENCES chat_tagged_memory(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='长期记忆的可核对来源'""",
    """CREATE TABLE IF NOT EXISTS chat_memory_operation (
      scope_id CHAR(64) NOT NULL COMMENT '写入权限作用域',
      request_id VARCHAR(128) NOT NULL COMMENT '原始请求标识',
      operation_id VARCHAR(128) NOT NULL COMMENT '幂等操作标识',
      arguments_digest CHAR(64) NOT NULL COMMENT '参数摘要防止复用身份篡改',
      operation_kind VARCHAR(20) NOT NULL COMMENT '写入或采用操作',
      result_json JSON NOT NULL COMMENT '仅状态和标识不复制正文',
      created_at DATETIME NOT NULL COMMENT '提交UTC时间',
      -- 复合主键在授权作用域内隔离请求与操作，防止重放写入。
      PRIMARY KEY (scope_id,request_id,operation_id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆写入与采用幂等回执'""",
    """CREATE TABLE IF NOT EXISTS chat_memory_embedding (
      memory_id INT NOT NULL COMMENT '事实标识',
      revision INT NOT NULL COMMENT '向量对应的事实版本',
      model_key VARCHAR(200) NOT NULL COMMENT '向量模型及配置版本',
      vector_json JSON NOT NULL COMMENT '有限数值向量数组',
      created_at DATETIME NOT NULL COMMENT '计算UTC时间',
      -- 复合主键禁止跨事实版本或模型版本复用向量。
      PRIMARY KEY (memory_id,revision,model_key),
      -- 外键保证向量仅附着于已有事实，遗忘仍由状态和版本筛选。
      FOREIGN KEY (memory_id) REFERENCES chat_tagged_memory(id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='现有数据库内的记忆向量缓存'""",
)


def sqlite_statement(statement: str) -> str:
    """只转换固定受控DDL，SQLite保留中文行注释说明字段与约束。"""
    result = statement.replace(
        "id INT NOT NULL AUTO_INCREMENT", "id INTEGER PRIMARY KEY AUTOINCREMENT"
    )
    result = re.sub(r"^\s*PRIMARY KEY \(id\),?\n", "", result, flags=re.M)
    result = re.sub(r"UNIQUE KEY \w+", "UNIQUE", result)
    result = re.sub(r"^\s*KEY \w+ \([^)]+\),?\n", "", result, flags=re.M)
    result = re.sub(
        r"^(\s*)([^\n]+?) COMMENT '([^']*)'(,?)$", r"\1-- \3\n\1\2\4", result, flags=re.M
    )
    result = re.sub(r"(\w+) JSON NOT NULL", r"\1 TEXT NOT NULL CHECK (json_valid(\1))", result)
    result = result.replace(
        "      UNIQUE (", "      -- 唯一约束防止同版本或来源重复。\n      UNIQUE ("
    )
    result = result.replace(
        "      FOREIGN KEY (", "      -- 外键保证辅助记录归属于已有事实。\n      FOREIGN KEY ("
    )
    result = result.replace(
        "      PRIMARY KEY (", "      -- 复合主键隔离不同事实版本和请求。\n      PRIMARY KEY ("
    )
    result = re.sub(r" ENGINE=InnoDB.*", "", result)
    return result


SQLITE_ADDITIONAL = (
    *(sqlite_statement(statement) for statement in MYSQL_ADDITIONAL),
    "-- 来源撤回按事件和会话定向定位，避免扫描并锁定无关证据。\n"
    "CREATE INDEX IF NOT EXISTS idx_memory_evidence_event "
    "ON chat_memory_evidence (source_event_id,source_session_id)",
)

# 索引列顺序是授权过滤与主题整理的查询契约，核验不能只检查索引同名存在。
MEMORY_INDEXES = {
    "idx_memory_status": "scope_id,status",
    "idx_memory_fingerprint": "scope_id,subject_id,content_fingerprint",
    "idx_memory_topic": "scope_id,subject_id,topic",
    "idx_memory_merged": "merged_into_id",
}
TABLE_INDEXES = {
    "chat_tagged_memory": MEMORY_INDEXES,
    "chat_memory_evidence": {"idx_memory_evidence_event": "source_event_id,source_session_id"},
}
TABLE_COLUMNS = {
    statement.split("(", 1)[0].split()[-1]: tuple(
        match.group(1)
        for match in re.finditer(r"^\s+(\w+) (?:INT|CHAR|VARCHAR|JSON|DATETIME)", statement, re.M)
    )
    for statement in MYSQL_ADDITIONAL
}
UNIQUE_KEYS = {
    "chat_memory_revision": (("id",), ("memory_id", "revision")),
    "chat_memory_evidence": (("id",), ("memory_id", "evidence_fingerprint")),
    "chat_memory_operation": (("scope_id", "request_id", "operation_id"),),
    "chat_memory_embedding": (("memory_id", "revision", "model_key"),),
}
JSON_COLUMNS = {
    "chat_memory_revision": "snapshot_json",
    "chat_memory_operation": "result_json",
    "chat_memory_embedding": "vector_json",
    "chat_session_context": "memory_facts_json",
}
