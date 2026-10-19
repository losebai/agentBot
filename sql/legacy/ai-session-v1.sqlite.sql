-- 用途：保留普通调用旧执行表原结构，供显式迁移和隔离测试核对。
-- 执行顺序：仅旧数据迁移参考；当前新库不应执行，不含删除或改写。
-- 普通文本调用记录
CREATE TABLE IF NOT EXISTS agent_bot_ai_session (
    -- 执行标识，三十二位十六进制
    execution_id TEXT NOT NULL,
    -- 区分大小写的会话标识
    session_id TEXT NOT NULL,
    -- 调用方请求标识
    request_id TEXT COLLATE NOCASE NOT NULL,
    -- 请求指定的模型别名
    configured_model TEXT COLLATE NOCASE NOT NULL,
    -- 实际使用的模型标识
    model TEXT COLLATE NOCASE NOT NULL,
    -- 执行状态：运行中、完成、失败或取消
    status TEXT COLLATE NOCASE NOT NULL,
    -- 执行耗时，单位毫秒
    duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
    -- 令牌用量统计
    `usage` TEXT COLLATE NOCASE NOT NULL CHECK (json_valid(usage)),
    -- 创建时间，协调世界时
    created_at DATETIME NOT NULL,
    -- 稳定错误代码
    error_code TEXT COLLATE NOCASE NULL,
    -- 用户输入文本
    prompt TEXT COLLATE NOCASE NOT NULL,
    -- 模型输出文本
    content TEXT COLLATE NOCASE NOT NULL,
    PRIMARY KEY (execution_id)
);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_session_created ON agent_bot_ai_session (session_id, created_at, execution_id);
-- 联合索引支持过滤及排序。
CREATE INDEX IF NOT EXISTS idx_created ON agent_bot_ai_session (created_at, execution_id);
