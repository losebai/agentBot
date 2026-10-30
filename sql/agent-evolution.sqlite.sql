-- 长期互动演化增量：完整旧业务库先完成已登记历史迁移，再由启动升级器执行本脚本。
-- 仅新增演化配置、匿名画像和因果事件，不改写原角色、凭据、记忆或历史数据。
-- 新库完整初始化含同样结构；表和索引允许中断后重入，不扫描历史 SQL。

-- 每个 Agent 的独立启停配置；无配置行时应用默认启用，空画像不改变旧行为。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_config (
    agent_id BIGINT PRIMARY KEY, -- 真实 Agent；随删除级联清理
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)), -- 暂停同时停止学习和应用
    revision BIGINT NOT NULL DEFAULT 1, -- 管理配置的乐观锁版本
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE
);

-- 画像按 Agent、匿名用户隔离，不能以一个群友的意见改变其他人的互动习惯。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_profile (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 稳定分页及审计标识
    agent_id BIGINT NOT NULL, -- 所属 Agent
    subject_key CHAR(64) NOT NULL, -- 会话密钥命名空间中的匿名用户摘要
    traits TEXT NOT NULL, -- 四个固定表达维度，值域为负三至三
    revision BIGINT NOT NULL DEFAULT 1, -- 行为变化和重置递增
    UNIQUE (agent_id,subject_key), -- 同一 Agent 的同一主体只有一个画像
    FOREIGN KEY (agent_id) REFERENCES agent_bot_agent(id) ON DELETE CASCADE
);

-- 原始反馈、实际变化和重置共享有序事件流，变化保留全部参与反馈的主键。
CREATE TABLE IF NOT EXISTS agent_bot_evolution_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 因果引用与分页主键
    profile_id BIGINT NOT NULL, -- 画像归属
    source_key VARCHAR(64) NOT NULL, -- 服务端真实事件匿名摘要，内部操作使用随机标识
    kind VARCHAR(16) NOT NULL CHECK (kind IN ('feedback','change','reset')), -- 事件类型
    dimension VARCHAR(16) NOT NULL, -- 固定维度；重置使用空串
    direction INTEGER NOT NULL CHECK (direction BETWEEN -1 AND 1), -- 明确反馈的方向
    excerpt VARCHAR(300) NOT NULL, -- 完整明确反馈句或固定变化原因
    observed_at DATETIME NOT NULL, -- UTC 来源时间，不由模型填写
    consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0,1)), -- 防止证据重复驱动变化
    before_value INTEGER NULL, -- 变化前档位，仅变化事件填写
    after_value INTEGER NULL, -- 变化后档位，仅变化事件填写
    evidence_ids TEXT NOT NULL, -- 变化所依据的真实反馈主键数组
    UNIQUE (profile_id,source_key,dimension,kind), -- 同一来源重放不重复累计
    FOREIGN KEY (profile_id) REFERENCES agent_bot_evolution_profile(id) ON DELETE CASCADE
);
-- 只读取当前画像、维度的未消费证据，并按来源时间选择九十天窗口。
CREATE INDEX IF NOT EXISTS idx_evolution_pending
    ON agent_bot_evolution_event(profile_id,dimension,kind,consumed,observed_at);
