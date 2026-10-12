-- Chat MCP 标签记忆。新设置默认启用，无须修改已有配置快照。
-- 表之间不设置外键，由应用在事务中维护关联。

CREATE TABLE IF NOT EXISTS dispatch_chat_memory_scope (
    scope_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '匿名作用域键',
    scope VARCHAR(16) NOT NULL COMMENT 'global/session/user/group',
    PRIMARY KEY (scope_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='标签记忆作用域与写锁';

CREATE TABLE IF NOT EXISTS dispatch_chat_tagged_memory (
    id INT NOT NULL AUTO_INCREMENT COMMENT '记忆自增主键',
    scope_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL COMMENT '关联作用域',
    memory_key VARCHAR(80) COLLATE utf8mb4_bin NOT NULL COMMENT '稳定事实键',
    content VARCHAR(1000) NOT NULL COMMENT '已确认的事实',
    is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '逻辑删除标记',
    create_time DATETIME NOT NULL COMMENT '创建时间',
    update_time DATETIME NOT NULL COMMENT '更新时间',
    PRIMARY KEY (id),
    UNIQUE KEY uk_tagged_memory_scope_key (scope_id, memory_key),
    KEY idx_tagged_memory_scope_time (scope_id, is_deleted, update_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='LLM 自主保存的标签记忆';

CREATE TABLE IF NOT EXISTS dispatch_chat_memory_tag (
    memory_id INT NOT NULL COMMENT '关联记忆',
    tag VARCHAR(32) COLLATE utf8mb4_bin NOT NULL COMMENT '规范化主题标签',
    PRIMARY KEY (memory_id, tag),
    KEY idx_memory_tag_lookup (tag, memory_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='记忆与标签多对多关联';
