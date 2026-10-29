-- 用途：为已完成旧版基线升级的 Agent 新增独立人格字段；目标版本 0.5.10。
-- 顺序：在既有登记增量后由启动升级器执行，SQLite 写事务整体提交或回滚。
-- 数据影响：所有既有角色、模型、工具、凭据与历史原样保留，旧 Agent 人格为空。
-- 独立人格设定，空字符串表示仅使用角色设定；已有该列时由升级器核验并跳过。
ALTER TABLE agent_bot_agent ADD COLUMN personality_prompt TEXT NOT NULL DEFAULT '';
