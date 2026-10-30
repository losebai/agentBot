-- 长期互动演化增量：完整旧业务库先完成已登记历史迁移，再由启动升级器执行本脚本。
-- 仅新增演化表，不改写原角色、凭据、记忆或历史；新库初始化包含相同结构。
-- CREATE TABLE IF NOT EXISTS 支持 MySQL 隐式提交后的重入；最终核验失败即停止启动。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_config (
    agent_id BIGINT UNSIGNED PRIMARY KEY COMMENT '真实 Agent；随删除级联清理',
    enabled TINYINT NOT NULL DEFAULT 1 COMMENT '暂停同时停止学习和应用',
    revision BIGINT NOT NULL DEFAULT 1 COMMENT '管理配置乐观锁版本',
    -- 外键防止孤立配置或 Agent 删除后身份复用。
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE,
    CHECK (enabled IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='Agent 演化启停配置；无行时默认启用';

CREATE TABLE IF NOT EXISTS agent_bot_evolution_profile (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '稳定分页和审计标识',
    agent_id BIGINT UNSIGNED NOT NULL COMMENT '所属 Agent',
    subject_key CHAR(64) NOT NULL COMMENT '会话密钥命名空间中的匿名用户摘要',
    traits TEXT NOT NULL COMMENT '四个固定表达维度；档位负三至三',
    revision BIGINT NOT NULL DEFAULT 1 COMMENT '实际行为变化或重置时递增',
    -- 唯一键隔离 Agent 与用户，禁止跨用户共享画像。
    UNIQUE KEY uq_evolution_subject (agent_id,subject_key),
    -- Agent 删除时释放对应画像，不能保留孤立人格。
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='按 Agent 和匿名用户隔离的演化状态';

CREATE TABLE IF NOT EXISTS agent_bot_evolution_event (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '因果引用及分页主键',
    profile_id BIGINT UNSIGNED NOT NULL COMMENT '所属画像',
    source_key VARCHAR(64) NOT NULL COMMENT '真实来源匿名摘要；内部操作用随机标识',
    kind VARCHAR(16) NOT NULL COMMENT 'feedback 反馈、change 变化、reset 重置',
    dimension VARCHAR(16) NOT NULL COMMENT '固定维度；重置为空串',
    direction TINYINT NOT NULL COMMENT '反馈方向负一、零或一',
    excerpt VARCHAR(300) NOT NULL COMMENT '完整明确反馈句或固定原因',
    observed_at DATETIME(6) NOT NULL COMMENT '真实来源 UTC 时间',
    consumed TINYINT NOT NULL DEFAULT 0 COMMENT '证据已消费后不能再次推动变化',
    before_value TINYINT NULL COMMENT '变化前档位',
    after_value TINYINT NULL COMMENT '变化后档位',
    evidence_ids TEXT NOT NULL COMMENT '参与本次变化的真实反馈主键数组',
    -- 去重索引保证请求重放不能虚增独立证据。
    UNIQUE KEY uq_evolution_source (profile_id,source_key,dimension,kind),
    -- 按画像和维度检索未消费的近期反馈。
    KEY idx_evolution_pending (profile_id,dimension,kind,consumed,observed_at),
    -- 画像删除时一并删除其证据，避免匿名主体跨生命周期复用。
    FOREIGN KEY (profile_id) REFERENCES agent_bot_evolution_profile(id) ON DELETE CASCADE,
    CHECK (kind IN ('feedback','change','reset')),
    CHECK (direction BETWEEN -1 AND 1),
    CHECK (consumed IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='演化原始反馈、因果变化和重置事件';
