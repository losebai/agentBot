-- 用途：为已完成旧版基线升级的 Agent 新增独立人格字段；目标版本 0.5.10。
-- 顺序：在既有登记增量后执行；先加可空列，仅回填该新列，再收紧为非空。
-- 数据影响：所有既有角色、模型、工具、凭据与历史原样保留，旧 Agent 人格为空。
-- MySQL DDL 会隐式提交；启动升级器核验列状态后跳过已完成步骤，支持中断重入。
ALTER TABLE agent_bot_agent
    ADD COLUMN personality_prompt MEDIUMTEXT NULL COMMENT '独立人格设定，空字符串表示仅使用角色设定';

UPDATE agent_bot_agent SET personality_prompt='' WHERE personality_prompt IS NULL;

ALTER TABLE agent_bot_agent
    MODIFY COLUMN personality_prompt MEDIUMTEXT NOT NULL COMMENT '独立人格设定，空字符串表示仅使用角色设定';
