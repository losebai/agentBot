-- 启动版本管理增量：用于已有 0.5.5–0.5.7 完整结构，先执行本脚本再运行已登记业务增量。
-- 仅新增版本与迁移流水表，不写版本号、不修改业务数据；运行器核验完成后登记版本。
CREATE TABLE IF NOT EXISTS agent_bot_schema_version (
    id TINYINT NOT NULL PRIMARY KEY COMMENT '单例版本记录，防止并行版本分叉',
    version VARCHAR(32) NOT NULL COMMENT '完成核验的程序稳定版本号',
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '版本推进时间',
    CONSTRAINT chk_schema_version_singleton CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='启动自动升级的数据库版本';
CREATE TABLE IF NOT EXISTS agent_bot_schema_migration (
    migration_id VARCHAR(128) NOT NULL PRIMARY KEY COMMENT '发布清单迁移唯一标识，主键防止重复登记',
    target_version VARCHAR(32) NOT NULL COMMENT '该迁移所属程序版本',
    checksum CHAR(64) NOT NULL COMMENT '当前后端 SQL 的 SHA256，禁止静默篡改历史迁移',
    status VARCHAR(16) NOT NULL COMMENT 'applying 执行中、applied 完成、failed 失败',
    applied_at DATETIME NULL COMMENT '迁移完成时间，未完成时为空',
    last_error VARCHAR(64) NULL COMMENT '仅保存固定错误码，不记录业务值或原始数据库异常',
    CONSTRAINT chk_schema_migration_status CHECK (status IN ('applying', 'applied', 'failed'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='启动迁移流水，不设业务外键以保留失败审计';
