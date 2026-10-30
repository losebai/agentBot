-- 可选角色关联升级：在 agent-evolution-v1 后执行，再核验完整初始化结构。
-- 新增范围、状态和记录，保留旧表及旧画像、事件 ID，不改写原角色或凭据。
-- 建表可重入，复制按稳定键去重；MySQL DDL 中断后只补齐未完成步骤。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_scope (
    scope_key VARCHAR(80) COLLATE utf8mb4_bin PRIMARY KEY COMMENT 'global 通用；agent:ID 为可选角色范围',
    agent_id BIGINT UNSIGNED NULL UNIQUE COMMENT '可选角色关联；删除只解除关联',
    enabled TINYINT NOT NULL DEFAULT 1 COMMENT '本范围学习和应用开关',
    revision BIGINT NOT NULL DEFAULT 0 COMMENT '配置乐观锁；零为未显式保存',
    -- 可选外键保留角色删除后的演化状态与历史。
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE SET NULL,
    CHECK (enabled IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='不依赖角色的通用或可选关联演化范围';
CREATE TABLE IF NOT EXISTS agent_bot_evolution_state (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '保留旧画像 ID',
    scope_key VARCHAR(80) COLLATE utf8mb4_bin NOT NULL COMMENT '所属演化范围',
    subject_key CHAR(64) NOT NULL COMMENT '真实用户匿名命名空间',
    traits TEXT NOT NULL COMMENT '固定表达维度的有界档位',
    revision BIGINT NOT NULL DEFAULT 1 COMMENT '行为变化和重置版本',
    -- 唯一索引防止同范围同用户产生多个状态。
    UNIQUE KEY uq_evolution_state_subject (scope_key,subject_key),
    -- 范围为状态的持久身份，不随角色删除。
    FOREIGN KEY (scope_key) REFERENCES agent_bot_evolution_scope(scope_key)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='通用或可选角色范围内的用户演化状态';
CREATE TABLE IF NOT EXISTS agent_bot_evolution_record (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY COMMENT '保留旧事件主键和因果引用',
    profile_id BIGINT UNSIGNED NOT NULL COMMENT '所属演化状态',
    source_key VARCHAR(64) NOT NULL COMMENT '真实来源匿名摘要',
    kind VARCHAR(16) NOT NULL COMMENT 'feedback 反馈、change 变化、reset 重置',
    dimension VARCHAR(16) NOT NULL COMMENT '固定维度；重置为空',
    direction TINYINT NOT NULL COMMENT '有界方向',
    excerpt VARCHAR(300) NOT NULL COMMENT '真实原话或固定原因',
    observed_at DATETIME(6) NOT NULL COMMENT '真实 UTC 来源时间或变化时间',
    consumed TINYINT NOT NULL DEFAULT 0 COMMENT '防止旧证据重复生效',
    before_value TINYINT NULL COMMENT '变化前档位',
    after_value TINYINT NULL COMMENT '变化后档位',
    evidence_ids TEXT NOT NULL COMMENT '真实反馈主键数组，迁移不重编号',
    -- 同状态同来源同维度去重，重放不能虚增证据。
    UNIQUE KEY uq_evolution_record_source (profile_id,source_key,dimension,kind),
    -- 状态机按维度、消费标记和来源时间选取证据。
    KEY idx_evolution_record_pending (profile_id,dimension,kind,consumed,observed_at),
    -- 管理列表按类型倒序分页。
    KEY idx_evolution_record_kind (kind,id),
    -- 外键保留状态与因果记录的归属，应用不提供删除审计入口。
    FOREIGN KEY (profile_id) REFERENCES agent_bot_evolution_state(id),
    CHECK (kind IN ('feedback','change','reset')),
    CHECK (direction BETWEEN -1 AND 1),
    CHECK (consumed IN (0,1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='跨演化范围可查询的因果记录';

-- 通用范围不需要任何角色，可直接学习和管理。
INSERT INTO agent_bot_evolution_scope (scope_key,agent_id,enabled,revision)
SELECT 'global',NULL,1,0 WHERE NOT EXISTS (
    SELECT 1 FROM agent_bot_evolution_scope WHERE scope_key='global'
);
-- 原关联作为可选范围保留，只补齐未复制的配置和状态，不覆盖新记录。
INSERT INTO agent_bot_evolution_scope (scope_key,agent_id,enabled,revision)
SELECT CONCAT('agent:',a.id),a.id,COALESCE(c.enabled,1),COALESCE(c.revision,0)
FROM agent_bot_agent a LEFT JOIN agent_bot_evolution_config c ON c.agent_id=a.id
WHERE NOT EXISTS (SELECT 1 FROM agent_bot_evolution_scope s WHERE s.agent_id=a.id);
INSERT INTO agent_bot_evolution_state (id,scope_key,subject_key,traits,revision)
SELECT p.id,CONCAT('agent:',p.agent_id),p.subject_key,p.traits,p.revision
FROM agent_bot_evolution_profile p
WHERE NOT EXISTS (SELECT 1 FROM agent_bot_evolution_state n WHERE n.id=p.id);
INSERT INTO agent_bot_evolution_record
    (id,profile_id,source_key,kind,dimension,direction,excerpt,observed_at,
     consumed,before_value,after_value,evidence_ids)
SELECT e.id,e.profile_id,e.source_key,e.kind,e.dimension,e.direction,e.excerpt,e.observed_at,
       e.consumed,e.before_value,e.after_value,e.evidence_ids
FROM agent_bot_evolution_event e
WHERE NOT EXISTS (SELECT 1 FROM agent_bot_evolution_record n WHERE n.id=e.id);
