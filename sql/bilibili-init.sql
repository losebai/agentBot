-- B 站单账号与外部操作记录；0.5.7 既有 MySQL 库由管理员显式执行一次。
-- 在完整初始化之后执行；仅新增表，不修改或删除业务数据，普通启动不执行升级。
CREATE TABLE IF NOT EXISTS agent_bot_bilibili_account (
    id TINYINT NOT NULL PRIMARY KEY COMMENT '固定单账号标识，只允许值 1',
    uid VARCHAR(20) NOT NULL COMMENT 'B 站账号 UID',
    name VARCHAR(200) NOT NULL COMMENT '最近验证的账号昵称',
    cookies TEXT NOT NULL COMMENT '仅服务端可读的登录凭据，不回填 API',
    updated_at BIGINT NOT NULL COMMENT '最近确认登录的 UTC 秒',
    CONSTRAINT chk_bilibili_account_singleton CHECK (id = 1)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='B 站单账号登录状态';
CREATE TABLE IF NOT EXISTS agent_bot_bilibili_operation (
    operation_key CHAR(64) NOT NULL PRIMARY KEY COMMENT '账号、归属、请求和参数的 SHA256 唯一占位',
    uid VARCHAR(20) NOT NULL COMMENT '操作绑定账号，清除登录时保留，不设级联外键',
    tool VARCHAR(64) NOT NULL COMMENT '产生外部副作用的工具名',
    status VARCHAR(16) NOT NULL COMMENT 'pending 代表进行中或结果未知，completed 代表已保存结果',
    result TEXT NULL COMMENT '成功后的安全 JSON 结果，不含凭据',
    created_at BIGINT NOT NULL COMMENT '操作占位的 UTC 秒',
    CONSTRAINT chk_bilibili_operation_status CHECK (status IN ('pending', 'completed'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='B 站外部操作幂等记录，主键仲裁并发，不自动重试未知结果';
