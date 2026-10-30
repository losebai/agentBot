-- 可选角色关联升级：在 agent-evolution-v1 完成后执行，再核验完整初始化结构。
-- 新增范围、状态与记录表，复制旧配置及证据 ID；旧三表原样保留，不重建或删除数据。
-- 新库初始化包含相同结构；所有插入按稳定键去重，允许中断后重入。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_scope (
    scope_key VARCHAR(80) PRIMARY KEY, -- global 为通用范围，agent:ID 为历史或可选角色范围
    agent_id BIGINT NULL UNIQUE, -- 可选角色关联；删除角色只解除关联
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)), -- 本范围的学习和应用开关
    revision BIGINT NOT NULL DEFAULT 0, -- 配置乐观锁；零代表尚未显式保存
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS agent_bot_evolution_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 保留旧画像 ID，保证历史链接有效
    scope_key VARCHAR(80) NOT NULL, -- 范围与用户共同隔离状态
    subject_key CHAR(64) NOT NULL, -- 真实用户的匿名命名空间
    traits TEXT NOT NULL, -- 四个固定表达维度的有界档位
    revision BIGINT NOT NULL DEFAULT 1, -- 行为变化和重置版本
    UNIQUE (scope_key,subject_key), -- 同一范围内用户只有一个状态
    FOREIGN KEY (scope_key) REFERENCES agent_bot_evolution_scope(scope_key)
);
CREATE TABLE IF NOT EXISTS agent_bot_evolution_record (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 保留旧事件主键和因果引用
    profile_id BIGINT NOT NULL, -- 所属演化状态
    source_key VARCHAR(64) NOT NULL, -- 服务端真实来源匿名摘要
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('feedback','change','reset')), -- 反馈、变化或重置
    dimension VARCHAR(16) NOT NULL, -- 固定维度，重置为空
    direction INTEGER NOT NULL CHECK (direction BETWEEN -1 AND 1), -- 有界方向
    excerpt VARCHAR(300) NOT NULL, -- 原始反馈或固定原因
    observed_at DATETIME NOT NULL, -- 真实 UTC 来源时间或变化时间
    consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0,1)), -- 防止旧证据重复生效
    before_value INTEGER NULL, -- 变化前档位
    after_value INTEGER NULL, -- 变化后档位
    evidence_ids TEXT NOT NULL, -- 真实反馈主键数组，迁移不重编号
    UNIQUE (profile_id,source_key,dimension,kind), -- 原始来源重放去重
    FOREIGN KEY (profile_id) REFERENCES agent_bot_evolution_state(id)
);
-- 按状态和维度选取未消费的九十天证据。
CREATE INDEX IF NOT EXISTS idx_evolution_record_pending
    ON agent_bot_evolution_record(profile_id,dimension,kind,consumed,observed_at);
-- 按事件类型和主键支持管理记录列表倒序分页。
CREATE INDEX IF NOT EXISTS idx_evolution_record_kind ON agent_bot_evolution_record(kind,id);

-- 无角色的新部署也具备可独立使用的通用范围。
INSERT INTO agent_bot_evolution_scope (scope_key,agent_id,enabled,revision)
SELECT 'global',NULL,1,0 WHERE NOT EXISTS (
    SELECT 1 FROM agent_bot_evolution_scope WHERE scope_key='global'
);
-- 只复制尚未登记的旧角色范围，不覆盖升级中断后已提交的新状态。
INSERT INTO agent_bot_evolution_scope (scope_key,agent_id,enabled,revision)
SELECT 'agent:' || a.id,a.id,COALESCE(c.enabled,1),COALESCE(c.revision,0)
FROM agent_bot_agent a LEFT JOIN agent_bot_evolution_config c ON c.agent_id=a.id
WHERE NOT EXISTS (SELECT 1 FROM agent_bot_evolution_scope s WHERE s.agent_id=a.id);
INSERT INTO agent_bot_evolution_state (id,scope_key,subject_key,traits,revision)
SELECT p.id,'agent:' || p.agent_id,p.subject_key,p.traits,p.revision
FROM agent_bot_evolution_profile p
WHERE NOT EXISTS (SELECT 1 FROM agent_bot_evolution_state n WHERE n.id=p.id);
INSERT INTO agent_bot_evolution_record
    (id,profile_id,source_key,kind,dimension,direction,excerpt,observed_at,
     consumed,before_value,after_value,evidence_ids)
SELECT e.id,e.profile_id,e.source_key,e.kind,e.dimension,e.direction,e.excerpt,e.observed_at,
       e.consumed,e.before_value,e.after_value,e.evidence_ids
FROM agent_bot_evolution_event e
WHERE NOT EXISTS (SELECT 1 FROM agent_bot_evolution_record n WHERE n.id=e.id);
