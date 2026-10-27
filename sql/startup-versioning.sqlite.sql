-- 启动版本管理增量：用于已有 0.5.5–0.5.7 完整结构，先执行本脚本再运行已登记业务增量。
-- 仅新增版本与迁移流水表，不写入版本号、不修改业务数据；运行器成功核验后登记版本。
CREATE TABLE IF NOT EXISTS agent_bot_schema_version (
    id INTEGER PRIMARY KEY CHECK (id = 1), -- 单例版本记录，防止并行版本分叉
    version TEXT NOT NULL, -- 完成核验的程序稳定版本号
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP -- 版本推进时间
);
CREATE TABLE IF NOT EXISTS agent_bot_schema_migration (
    migration_id TEXT PRIMARY KEY, -- 发布清单中的唯一迁移 ID，主键避免重复登记
    target_version TEXT NOT NULL, -- 该迁移所属程序版本
    checksum TEXT NOT NULL, -- 当前后端 SQL 的 SHA256，禁止静默篡改历史迁移
    status TEXT NOT NULL CHECK (status IN ('applying', 'applied', 'failed')), -- 中断可审计
    applied_at DATETIME NULL, -- 迁移完成时间，未完成时为空
    last_error TEXT NULL -- 仅保存固定错误码，不记录业务值或原始数据库异常
);
